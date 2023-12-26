# Use an official NVIDIA runtime as a parent image
FROM nvcr.io/nvidia/pytorch:22.04-py3
# Set the working directory in the container
USER root
ARG user=YiVal_test
RUN apt-get update && apt-get install -y sudo
RUN useradd --create-home --no-log-init --shell /bin/bash ${user}
    #&& groupadd sudo \
RUN usermod -a -G sudo ${user}
RUN adduser ${user} sudo
RUN echo "${user}:121212" | chpasswd
RUN usermod -u 1000 ${user} && usermod -G 1000 ${user}

## Install Python3 
## TimeZone Settings
ARG DEBIAN_FRONTEND=noninteractive
ENV TZ=America/Los_Angeles
RUN apt-get install -y tzdata
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone
RUN apt-get update
RUN apt upgrade -y
RUN apt-get install -y --no-install-recommends \
    software-properties-common \
    git \
    wget \
    curl \
    build-essential \
    libffi-dev \
    libgdbm-dev \
    libc6-dev \
    libssl-dev \
    zlib1g-dev \
    libbz2-dev \
    libreadline-dev \
    libsqlite3-dev \
    libncursesw5-dev \
    xz-utils \
    tk-dev \
    libxml2-dev \
    libxmlsec1-dev \
    liblzma-dev \
    vim

RUN add-apt-repository ppa:deadsnakes/ppa
## Install tini
#RUN dpkgArch="$(dpkg --print-architecture)" \s
#  && curl -sL https://github.com/krallin/tini/releases/download/v0.19.0/tini-${dpkgArch} -o /usr/local/bin/tini \
#  && chmod +x /usr/local/bin/tini

## Install Python3
RUN apt-get purge -y python python3 
RUN apt-get update
RUN apt-get install -y --no-install-recommends python3.10 python3.10-dev\
    python3-pip \
    python3-distutils \
    python3-setuptools \
    python3-wheel

#RUN apt-get install -y python3.10-dev
RUN pip3 install poetry
RUN rm -rf /tmp/* && apt-get clean
RUN pip3 install poetry
RUN python3 -V
RUN rm -rf /tmp/* 

# Copy the pyproject.toml file into the container at /usr/src/app
# You should create a pyproject.toml file in your project folder that specifies Jupyter Lab as a dependency
#COPY pyproject.toml ./

# Install dependencies using Poetry
#RUN poetry install

#WORKDIR /usr/src/app
USER ${user}
WORKDIR /home/${user}

RUN git clone -b stable https://github.com/YiVal/YiVal.git
## Unfortunately the dependency of python packages is broken for py3.11, run poetry to install py3.10
RUN poetry config virtualenvs.create true
RUN cd YiVal && poetry install --no-ansi
RUN cd YiVal && poetry add -D openai==0.27.10 requests jupyterlab

## Native jupyterlab require to build ipykernel first by poetry and then use
RUN pip3 install jupyterlab
RUN export PATH=/home/${user}/.local/bin:$PATH >> /home/${user}/.bashrc
RUN cd YiVal && poetry run ipython kernel install --user --name=py310_foryival
RUN poetry cache clear PyPI --all && poetry cache clear _default_cache --all
RUN /bin/bash .profile
# Run Jupyter Lab
#ENTRYPOINT ["/usr/local/bin/tini", "--"]
#CMD ["pwd"]
#CMD ["ls"]
WORKDIR /home/${user}
COPY Demo_test_headlinegen.ipynb /home/${user}/
# Make port 8888 available to the world outside this container
EXPOSE 8888
EXPOSE 80
EXPOSE 22
#CMD [ "export PATH=$HOME/.local/bin:$PATH >> $HOME/.bashrc && . $HOME/.bashrc"]
#ENTRYPOINT [ "/bin/bash" , "/home/YiVal_test/.profile"]