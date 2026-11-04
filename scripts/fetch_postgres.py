"""Download the pinned official PostgreSQL image through the project proxy."""

from pathlib import Path

from fetch_rustfs import main as load_verified_image


if __name__ == "__main__":
    load_verified_image(
        image="postgres:18.6-trixie",
        cache=Path("/opt/myblog-services/postgres-image"),
        index_digest="sha256:3a4264520e121176619f54ff867d12774bc81e95e7b84db58d63bccdb6144b81",
        manifest_digest="sha256:f4b419e350fd98d4a8429603295208976dea4c8fb8326d818d346d3e29f1db6f",
        config_digest="sha256:a1be1c8380f5c8ca80b4ad72f6be191f8a283e0ecb2e9496b1ee67dc0022d0f1",
        registry="https://registry-1.docker.io/v2/library/postgres/",
    )
