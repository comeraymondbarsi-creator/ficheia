# Image officielle Microsoft Playwright — inclut Chromium + toutes les libs système
FROM mcr.microsoft.com/playwright/python:v1.59.0-jammy

WORKDIR /app

# Dépendances Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Code source
COPY . .

# Chromium est déjà installé dans l'image de base
# On s'assure quand même que le bon browser est lié à cette version de playwright
RUN playwright install chromium

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
