
# This stage installs build dependencies and compiles Python packages.
# It will be discarded in the final image, keeping only the compiled packages.
FROM python:3.14-slim-bookworm AS builder

# Install system packages required to build Python packages.
RUN apt-get update --yes --quiet && apt-get install --yes --quiet --no-install-recommends \
    build-essential \
    libpq-dev \
    libjpeg62-turbo-dev \
    zlib1g-dev \
    libwebp-dev \
 && rm -rf /var/lib/apt/lists/* \
 && python -m venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH"

# Install the project requirements.
COPY requirements.txt /
RUN pip install -r /requirements.txt

# Install the application server.
RUN pip install "gunicorn==25.1.0"


# RUNTIME STAGE
# Use an official Python runtime based on Debian 12 "bookworm" as a parent image.
FROM python:3.14-slim-bookworm AS runtime

# Install runtime system packages required by Wagtail and Django.
# These are the runtime libraries needed by the compiled Python packages.
RUN apt-get update --yes --quiet && apt-get install --yes --quiet --no-install-recommends \
    libpq5 \
    libjpeg62-turbo \
    libwebp7 \
 && rm -rf /var/lib/apt/lists/*

# Add user that will be used in the container.
RUN useradd wagtail

# Port used by this container to serve HTTP.
EXPOSE 8000

# Set environment variables.
# 1. Force Python stdout and stderr streams to be unbuffered.
# 2. Set PORT variable that is used by Gunicorn. This should match "EXPOSE"
#    command.
# 3. Add the virtual environment to PATH.
ENV PYTHONUNBUFFERED=1 \
    PORT=8000 \
    PATH="/opt/venv/bin:$PATH"



# Copy the virtual environment from the builder stage.
COPY --from=builder /opt/venv /opt/venv

# Use /app folder as a directory where the source code is stored.
WORKDIR /app

# Own application files; PostgreSQL data lives in the separate service volume.
RUN chown wagtail:wagtail /app

# Copy the source code of the project into the container.
COPY --chown=wagtail:wagtail . .

# Use user "wagtail" to run the build commands below and the server itself.
USER wagtail

# Collect static files.
RUN python manage.py collectstatic --noinput --clear

# Schema migrations are an explicitly authorized release operation.
CMD ["gunicorn", "myblog.wsgi:application"]
