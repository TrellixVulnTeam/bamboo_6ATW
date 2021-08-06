FROM pytorch/pytorch:1.9.0-cuda11.1-cudnn8-devel
RUN apt-get update && apt-get install -y libaio-dev
COPY . /workspace
ENV VIRTUAL_ENV=/workspace/venv
RUN python -m venv --system-site-packages $VIRTUAL_ENV
ENV PATH=$VIRTUAL_ENV/bin:/workspace/external/deepspeed/bin:$PATH
ENV PYTHONPATH=/workspace:/workspace/external/deepspeed
RUN pip install -U pip && pip install -r /workspace/requirements.txt
RUN cd /workspace/external/apex && pip install -v --disable-pip-version-check --no-cache-dir --global-option="--cpp_ext" --global-option="--cuda_ext" ./
RUN echo "0.1.0-169" > /workspace/VERSION
