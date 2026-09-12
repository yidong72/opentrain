FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY open_train ./open_train
RUN pip install --no-cache-dir . && useradd --create-home tracker && mkdir /data && chown tracker:tracker /data
USER tracker
ENV OPEN_TRAIN_DATA_DIR=/data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
ENTRYPOINT ["open-train", "serve", "--host", "0.0.0.0"]
