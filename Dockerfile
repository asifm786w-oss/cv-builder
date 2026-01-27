FROM python:3.12-slim

# 1) System deps needed by Playwright/Chromium (this fixes libglib-2.0.so.0)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2 libx11-6 libx11-xcb1 libxcb1 \
    libxcb-dri3-0 libxcb-dri2-0 libxcb-glx0 libxcb-shm0 libxcb-render0 libxrender1 \
    libxext6 libxtst6 libxi6 fonts-liberation ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 2) Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3) Install Playwright browsers into a known path
ENV PLAYWRIGHT_BROWSERS_PATH=/app/.playwright
RUN python -m playwright install chromium chromium-headless-shell

# 4) App
COPY . .

# Railway gives you $PORT
CMD ["bash", "-lc", "python -m streamlit run App.py --server.address 0.0.0.0 --server.port ${PORT:-8080} --server.headless true"]
