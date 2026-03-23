FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

ENV HOST=0.0.0.0
ENV PORT=8080
ENV STOCK_TRACKER_DATA_DIR=/data

EXPOSE 8080

CMD ["python", "stock_tracker_app.py"]
