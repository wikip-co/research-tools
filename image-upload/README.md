# image-upload

Small CLI tool for agent-friendly Cloudinary image operations. All commands return JSON.

## What It Does

- Upload local files to Cloudinary
- Check whether an asset already exists by `public_id`
- Search images by `public_id` prefix or Cloudinary tag
- Download either the transformed site image or the original uploaded asset

## Setup

The CLI reads credentials from a local `.env` file automatically, or from exported shell variables.
It looks in the current working directory first and then in the tool directory.

Required variables:

```bash
export CLOUDINARY_CLOUD_NAME=your_cloud_name
export CLOUDINARY_API_KEY=your_api_key
export CLOUDINARY_API_SECRET=your_api_secret
```

Install dependencies:

```bash
uv sync
```

Run via Docker Compose:

```bash
docker compose run --rm image-upload --help
```

## Usage

Upload a file:

```bash
uv run image-upload /path/to/photo.jpg
```

Upload with a fixed `public_id` and validate the site-style transformed URL:

```bash
uv run image-upload /path/to/photo.jpg --public-id lemon --validate-url
```

Upload into a folder:

```bash
uv run image-upload /path/to/photo.jpg --folder agent-uploads --public-id my-photo
```

Check whether a `public_id` already exists:

```bash
uv run image-upload --check-only --public-id lemon
```

Search by `public_id` prefix:

```bash
uv run image-upload --search-prefix lem --max-results 5
```

Search by tag:

```bash
uv run image-upload --search-tag citrus --max-results 5
```

Download the transformed site image:

```bash
uv run image-upload --download --public-id lemon --output-dir /tmp/cloudinary-images
```

Download the original uploaded asset:

```bash
uv run image-upload --download --download-original --public-id lemon --output-dir /tmp/cloudinary-images-original
```

## Docker Compose Examples

Check whether an asset exists:

```bash
docker compose run --rm image-upload --check-only --public-id lemon
```

Upload a file from the current working directory:

```bash
docker compose run --rm -v "$PWD:/work" image-upload /work/photo.jpg --public-id lemon
```

Search by tag:

```bash
docker compose run --rm image-upload --search-tag citrus --max-results 5
```

## Agent Contract

This project is designed for agent use through a small CLI surface that always writes JSON.

Environment requirements:

- `CLOUDINARY_CLOUD_NAME`
- `CLOUDINARY_API_KEY`
- `CLOUDINARY_API_SECRET`

Preferred invocation forms:

- `uv run image-upload ...`
- `docker compose run --rm image-upload ...`

Supported command patterns:

- Upload: `image-upload /path/to/file.jpg [--folder FOLDER] [--public-id ID] [--tag TAG] [--overwrite] [--validate-url]`
- Existence check: `image-upload --check-only --public-id ID [--resource-type image|video]`
- Search by prefix: `image-upload --search-prefix PREFIX [--max-results N] [--resource-type image|video]`
- Search by tag: `image-upload --search-tag TAG [--max-results N] [--resource-type image|video]`
- Download: `image-upload --download --public-id ID [--output-dir DIR] [--download-original] [--resource-type image|video]`

Response contract:

- Success is printed to stdout as `{"ok": true, "result": ...}`
- Failures are printed to stderr as `{"ok": false, "error": "..."}`
- Exit code `0` indicates success
- Exit code `1` indicates runtime failure

Operational notes for agents:

- `--search-prefix` and `--search-tag` are mutually exclusive.
- `--check-only` and `--download` both require `--public-id`.
- Uploads detect resource type automatically.
- Downloads default to the transformed image URL for image assets unless `--download-original` is set.
- `--resource-type` defaults to `image`.
- The CLI reads a local `.env` file automatically if present.
- The default image delivery URL format is `https://res.cloudinary.com/<cloud_name>/image/upload/w_200,f_auto/<public_id>`.
- For new wiki articles, prefer setting frontmatter `image:` explicitly even if the filename-based theme fallback would work.

Example upload result:

```json
{
  "ok": true,
  "result": {
    "local_path": "/path/to/photo.jpg",
    "exists_before_upload": false,
    "public_id": "lemon",
    "resource_type": "image",
    "secure_url": "https://res.cloudinary.com/...",
    "url": "http://res.cloudinary.com/...",
    "delivery_url": "https://res.cloudinary.com/alchemist-cookbook/image/upload/w_200,f_auto/lemon",
    "delivery_url_valid": true
  }
}
```

Example existence check result:

```json
{
  "ok": true,
  "result": {
    "public_id": "lemon",
    "exists": true,
    "delivery_url": "https://res.cloudinary.com/alchemist-cookbook/image/upload/w_200,f_auto/lemon"
  }
}
```

Example download result:

```json
{
  "ok": true,
  "result": {
    "public_id": "Lemon",
    "resource_type": "image",
    "download_type": "original",
    "source_url": "https://res.cloudinary.com/alchemist-cookbook/image/upload/v1539395623/Lemon.jpg",
    "local_path": "/tmp/cloudinary-images-original/Lemon.jpg",
    "bytes": 309688
  }
}
```
