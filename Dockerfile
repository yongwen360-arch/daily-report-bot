FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app.py .
RUN mkdir -p data
EXPOSE 8888
CMD ["gunicorn", "app:app", "-b", "0.0.0.0:8888", "-w", "2", "--timeout", "60"]
