# Releasing MeshCenter

A short checklist; the details of each step live in the PRs and notes of earlier releases (`docs/releases/`).

1. Everything for the release is merged to `main` and CI is green.
2. Write `docs/releases/vX.Y.Z.md` (range `vPREVIOUS..main`, what changed, whether any dependency, systemd, sudoers,
   udev, helper or installer file changed, and a note when a database schema changes: say whether the previous code
   runs on the new schema). Bump the version in `docs/Docker_Installation.md`. Merge that as its own PR.
3. Before tagging, diff `templates/index.html` against the previous tag: only intended additions and the cache token.
4. Annotated tag `vX.Y.Z` on the merge commit, GitHub release with the notes file as its body (the release workflow
   attaches `install.sh` and `meshcenter-firstboot.sh`).
5. Update the nodes through **Workspace > System > Updates** (Check now, then Update).
6. Docker: `deploy/docker/build.sh` from a checkout of the tag, `docker save`, `sha256sum`, and keep the tar and the
   checksum outside the repository.
7. **ffmpeg (Docker image):** the image builds ffmpeg 7.1.x from a pinned source (`FFMPEG_VERSION` and `FFMPEG_SHA256` in
   the `Dockerfile`). When a security point release of the 7.1 line appears, bump both values, rebuild the image and run
   `python scripts/check_docker_ffmpeg.py meshcenter:<version>` (it must print `GATE PASSED`). Write the pinned
   ffmpeg version into the release notes of every release that ships a rebuilt image.
