# The owner app. Built on the venue image so it can write a venue's admin
# password exactly as the venue would (pikaraoke.lib.admin_auth).
FROM karaoke

# Root: it talks to the Docker socket and hands each venue's files to uid 1000.
USER root
RUN uv pip install --no-cache-dir docker==7.2.0

WORKDIR /cloud
COPY owner.py ./
COPY templates ./templates

ENTRYPOINT ["python", "owner.py"]
