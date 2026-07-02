FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY hugo_common.py hugo_summarize.py hugo_research.py hugo_features.py hugo_queue.py hugo_curator.py daily_digest.py hugo_bot.py ./

# State directory inside the container — mount a host volume here for persistence.
RUN mkdir -p /root/.hugo

# Default: start the persistent bot. Override with `python daily_digest.py` for the scheduled job.
CMD ["python", "hugo_bot.py"]
