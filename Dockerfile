FROM quay.io/ansible/creator-ee@sha256:335246cec55cde727eba150a4bcaa735bb2bc51d24d7410ff26a512f83871877

WORKDIR /ansible/project
ENV PYTHONUNBUFFERED=1
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

ENTRYPOINT ["python3", "scraper.py"]
