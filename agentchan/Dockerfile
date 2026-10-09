FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY tests ./tests
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "app.server"]
