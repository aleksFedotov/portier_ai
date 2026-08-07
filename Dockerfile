FROM python:3.12-slim

# Шрифт с кириллицей для PDF-счетов (reportlab)
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir .

# Прогрев natasha при сборке: проверяем, что NamesExtractor готов,
# чтобы контейнер не делал этого при каждом старте
RUN python -c "from natasha import NamesExtractor, MorphVocab; NamesExtractor(MorphVocab())"

CMD ["python", "-m", "portier"]
