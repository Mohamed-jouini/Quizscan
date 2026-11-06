FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

# Tesseract OCR (français + arabe) — seule dépendance système de l'application
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-fra tesseract-ocr-ara \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt "gunicorn>=23,<27"

# Tesseract tourne en plusieurs exemplaires à la fois (lecture des copies, OCR d'un
# questionnaire en PDF sans texte, 4 fils chacune) : un fil chacun, sinon leurs
# fils OpenMP se disputent les processeurs et tout ralentit. Placé après les
# paquets : le changer ne les fait pas réinstaller.
ENV OMP_THREAD_LIMIT=1

COPY . .

RUN chmod +x docker-entrypoint.sh
EXPOSE 8000
CMD ["./docker-entrypoint.sh"]
