FROM python:3.12-slim

WORKDIR /app

# Install deps first (cached until pyproject changes), then the package.
COPY pyproject.toml ./
COPY rbga ./rbga
RUN pip install --no-cache-dir .

EXPOSE 8000

# Default = API. compose overrides `command:` for the bot (python -m rbga.bot).
CMD ["uvicorn", "rbga.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
