# The example api image. Plain pip install (no lockfile) keeps the fixture readable;
# a real consumer would pin via uv/pip-tools.
FROM docker.io/library/python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir \
      "fastapi==0.115.*" \
      "uvicorn[standard]==0.34.*" \
      "psycopg[binary]==3.2.*"

COPY main.py .

EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
