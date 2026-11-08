FROM python:3.13-alpine@sha256:9cf2fdca7b3dd6082fd160ff5b8664f108c7441e200018a36554b2e8028f45df
WORKDIR /app
COPY bot.py booking.py qr_bridge.py /app/
COPY certs /app/certs
ENV PYTHONUNBUFFERED=1
CMD ["python", "/app/bot.py"]
