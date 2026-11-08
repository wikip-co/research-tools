.PHONY: sync test test-root test-core test-gmail test-web test-wiki test-image

sync:
	uv sync --all-packages

test: test-root test-core test-gmail test-web test-wiki test-image

test-root:
	uv run python -m unittest discover -s tests -v

test-core:
	uv run python -m unittest discover -s content-agent-core/tests -v

test-gmail:
	uv run --directory gmail-reader python -m unittest discover -s tests -v

test-web:
	uv run --directory web-scraper python -m unittest discover -s tests -v

test-wiki:
	uv run --directory wiki-automation python -m unittest discover -s tests -v

test-image:
	uv run --directory image-upload python -m unittest discover -s tests -v
