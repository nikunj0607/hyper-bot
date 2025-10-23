FROM python:3.10-slim

# Prevent interactive prompts
ENV DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# Copy project
COPY . /app

# Install deps
RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# Expose port for render
EXPOSE 10000

CMD ["bash", "start.sh"]
