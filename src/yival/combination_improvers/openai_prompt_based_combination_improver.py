import copy
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

from tqdm import tqdm

from ..experiment.evaluator import Evaluator
from ..experiment.rate_limiter import RateLimiter
from ..experiment.utils import generate_experiment, run_single_input
from ..logger.token_logger import TokenLogger
from ..schemas.combination_improver_configs import (
    OpenAIPromptBasedCombinationImproverConfig,
)
from ..schemas.common_structures import InputData
from ..schemas.evaluator_config import OpenAIPromptBasedEvaluatorConfig
from ..schemas.experiment_config import (
    CombinationAggregatedMetrics,
    Experiment,
    ExperimentConfig,
    ExperimentResult,
    ImproverOutput,
    WrapperConfig,
    WrapperVariation,
)
from ..states.experiment_state import ExperimentState
from .base_combination_improver import BaseCombinationImprover

PROMPT = """
Given the evaluator results, scales, and descriptions, please analyze and suggest improvements to the provided combinations.
The combinations are presented in the format {key: value}, where 'value' can represent prompts or other parameters. You
can define the specifics of the value as needed.
After your analysis, the combination will be re-evaluated by the evaluators, and feedback will be provided. For context,
we will supply the prior combinations if available. Keey the original {key: value} and keep the original key,
only update the value if needed (keep the orignal value if no update is needed). The value will be of the same types
For example: {"keyA" : "ValueA", "KeyB": 1} if you think keyB should be and not KeyA output 
format should be {"keyA" : "ValueA", "KeyB": 2}, make the outptu so that it can be pared with pyhon directly using eval
"""

COT = """
First, write out in a step by step manner your reasoning to be sure that your conclusion is correct. 
Avoid simply stating the correct answer at the outset. Reasoning:
"""


def find_best_combination(
    experiment: Experiment
) -> CombinationAggregatedMetrics | None:
    if not experiment.combination_aggregated_metrics or len(
        experiment.combination_aggregated_metrics
    ) == 0:
        return None

    if experiment.selection_output:
        combo_key = experiment.selection_output.best_combination
    else:
        combo_key = experiment.combination_aggregated_metrics[0].combo_key

    for metric in experiment.combination_aggregated_metrics:
        if metric.combo_key == combo_key:
            return metric
    return None


def get_evaluator_config(
    config: ExperimentConfig
) -> List[OpenAIPromptBasedEvaluatorConfig]:
    return [
        c for c in config["evaluators"]  # type: ignore
        if c["name"] == "openai_prompt_based_evaluator"
    ]


def find_evaluator_results(
    configs: List[OpenAIPromptBasedEvaluatorConfig],
    metrics: CombinationAggregatedMetrics | None
) -> str:
    res: str = ""
    if not metrics:
        return res
    for k, v in metrics.aggregated_metrics.items():
        for evaluator_config in configs:
            key = evaluator_config["name"]  # type: ignore
            key += ": " + evaluator_config[
                "display_name"] if evaluator_config[  # type: ignore
                    "display_name"] else ""  # type: ignore
            if k == key:
                metric_results = [
                    f"{metric.name},{metric.value}" for metric in v
                ]
                metric_results_string = ', '.join(metric_results)
                res += "\n\n"
                res += "Evaluator Results: " + metric_results_string + "\n"
                res += "Scale: " + evaluator_config["scale_description"
                                                    ] + "\n"  # type: ignore
                res += "Description: " + evaluator_config[
                    "description"] + "\n"  # type: ignore
    return res


def construct_prompt(
    evaluator_outputs: str,
    current_combination: str,
    prior_iterations: Optional[List[str]] = None
) -> str:
    prompt = PROMPT + "\n\n" + evaluator_outputs + "\n\n"
    prompt += "Current Combination: " + current_combination + "\n\n"
    if prior_iterations and len(prior_iterations) > 0:
        prompt += "Prior Iterations: \n\n"
        for iteration in prior_iterations:
            prompt += iteration + "\n\n"
    prompt += COT
    return prompt


def extract_dict_from_string(s: str) -> str | None:
    """
    Extract the outermost dictionary from a string, handling nested dictionaries.
    """
    open_braces = 0
    dict_start = s.find('{')
    for i in range(dict_start, len(s)):
        if s[i] == '{':
            open_braces += 1
        elif s[i] == '}':
            open_braces -= 1
        if open_braces == 0:
            return s[dict_start:i + 1]
    return None  # Return None if no matching dictionary found


rate_limiter = RateLimiter(10 / 60)


class OpenAIPromptBasedCombinationImprover(BaseCombinationImprover):
    """
    Combination improver that uses OpenAI's model to improve the combination.
    """
    default_config: OpenAIPromptBasedCombinationImproverConfig = OpenAIPromptBasedCombinationImproverConfig(
        openai_model_name="gpt-4",
        max_iterations=3,
        stop_conditions={
            "openai_prompt_based_evaluator: clarity": 2.0,
            "openai_prompt_based_evaluator: relevance": 3,
            "openai_prompt_based_evaluator: catchiness": 3
        }
    )

    def __init__(self, config: OpenAIPromptBasedCombinationImproverConfig):
        super().__init__(config)
        self.config = config

    def parallel_task(self, d, all_combinations, state, logger, evaluator):
        rate_limiter()
        return run_single_input(
            d,
            self.updated_config,
            all_combinations=all_combinations,
            state=state,
            logger=logger,
            evaluator=evaluator
        )

    def check_if_done(self, experiment: Experiment) -> bool:
        combo = experiment.combination_aggregated_metrics
        condition_met = []
        average_value: float = 0.0
        for k, v in combo[0].aggregated_metrics.items():
            if "stop_conditions" in self.config and k in self.config[  # type: ignore
                "stop_conditions"]:
                average_value += v[
                    0].value  # Assume we only have one metrics calculation.
                if self.config["stop_conditions"][k] <= v[  # type: ignore
                    0].value:
                    condition_met.append(True)
        if len(condition_met) > 0 and len(condition_met) == len(
            self.config["stop_conditions"]  # type: ignore
        ):
            return True
        if "average_score" in self.config and self.config[  # type: ignore
            "average_score"
        ] <= average_value / len(combo[0].aggregated_metrics):
            return True
        return False

    def improve(
        self, experiment: Experiment, config: ExperimentConfig,
        evaluator: Evaluator, logger: TokenLogger
    ) -> ImproverOutput:
        experiments: List[Experiment] = []
        prior_iterations: List[str] = []
        self.updated_config = copy.deepcopy(config)
        original_combo_key = ''
        results: List[ExperimentResult] = []
        data: List[InputData] = []
        self.updated_config["variations"] = []  # type: ignore
        for i in range(self.config["max_iterations"]):  # type: ignore
            print(f"Start iteration {i}...")
            current_iteration_results: List[ExperimentResult] = []
            configs = get_evaluator_config(config)
            combo = find_best_combination(experiment)
            if not combo:
                continue
            if len(results) == 0 and combo:
                original_combo_key = combo.combo_key
                for r in combo.experiment_results:
                    r.input_data.content.pop("raw_output", None)
                    results.append(r)
                results.extend(combo.experiment_results)
            evaluator_result = find_evaluator_results(configs, combo)
            message = [{
                "role":
                "user",
                "content":
                construct_prompt(
                    evaluator_result, combo.combo_key, prior_iterations
                )
            }]
            prior_iterations.append(combo.combo_key)
            while True:
                try:
                    import openai
                    response = openai.ChatCompletion.create(
                        model="gpt-4", messages=message, max_tokens=5000
                    )
                    exper = extract_dict_from_string(
                        response["choices"][0]["message"]["content"]
                    )
                    if exper:
                        extracted_dict = eval(exper)
                    else:
                        continue
                    break
                except Exception as e:
                    print("Retrying: ", e)

            for k, v in extracted_dict.items():
                # TODO: Support custom class value_type
                self.updated_config["variations"].append(( # type: ignore
                    WrapperConfig(
                        name=k,
                        variations=[
                            WrapperVariation(
                                value=v, value_type=str(type(v)).split("'")[1]
                            )
                        ]
                    )
                ))
            state = ExperimentState.get_instance()
            state.clear_variations_for_experiment()
            state.set_experiment_config(self.updated_config)
            state.active = True
            all_combinations = state.get_all_variation_combinations()
            total = len(combo.experiment_results)
            data.clear()
            for r in combo.experiment_results:
                input_data = copy.deepcopy(r.input_data)
                input_data.content.pop("raw_output", None)
                data.append(input_data)
            with tqdm(total=total, desc="Processing", unit="item") as pbar:
                with ThreadPoolExecutor() as executor:
                    for res in executor.map(
                        self.parallel_task, data,
                        [all_combinations] * len(data), [state] * len(data),
                        [logger] * len(data), [evaluator] * len(data)
                    ):
                        current_iteration_results.extend(res)
                        pbar.update(len(res))
            experiment = generate_experiment(
                current_iteration_results, evaluator
            )
            if self.check_if_done(experiment):
                break
            experiments.append(experiment)

        for exp in experiments:
            for res in exp.combination_aggregated_metrics:
                tmp = []
                for r in res.experiment_results:
                    r.input_data.content.pop("raw_output", None)
                    tmp.append(r)
                results.extend(res.experiment_results)
        experiment = generate_experiment(
            results, evaluator, evaluate_group=False
        )
        improver_output = ImproverOutput(
            group_experiment_results=experiment.group_experiment_results,
            combination_aggregated_metrics=experiment.
            combination_aggregated_metrics,
            original_best_combo_key=original_combo_key
        )
        return improver_output


BaseCombinationImprover.register_combination_improver(
    "openai_prompt_based_combination_improver",
    OpenAIPromptBasedCombinationImprover,
    OpenAIPromptBasedCombinationImproverConfig
)