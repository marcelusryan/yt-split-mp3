FROM python:3.9-slim
ENV PYTHONUNBUFFERED=1

# install git & ffmpeg
RUN apt-get update \
  && apt-get install -y --no-install-recommends git ffmpeg \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# install everything except yt-dlp
COPY requirements.txt ./
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

# uninstall any existing yt-dlp, then pull master
RUN pip uninstall -y yt-dlp \
  && pip install --no-cache-dir --root-user-action=ignore \
      --upgrade --force-reinstall \
      git+https://github.com/yt-dlp/yt-dlp.git@master

COPY . .

EXPOSE 5000
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]
