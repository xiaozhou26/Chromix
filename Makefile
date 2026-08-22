# Fortress developer tasks. These wrap the scripts that already live in the repo;
# nothing here compiles Chromium (see docs/BUILD_NATIVE.md for that).
.DEFAULT_GOAL := help
PYTHON ?= python3
BUNDLE ?= dist/tilion-fortress

.PHONY: help lint test check gauntlet apply bundle clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

lint: ## Run the patch-set integrity linter
	$(PYTHON) tools/check_patches.py

test: ## Run the Python SDK unit tests
	$(PYTHON) -m pytest sdk/python/tests -q

check: lint test ## Lint + test (what CI gates on)

gauntlet: ## Run the live detection gauntlet against a bundle (BUNDLE=/path/to/tilion-fortress)
	$(PYTHON) tools/gauntlet.py --bundle $(BUNDLE)

apply: ## Apply the patch series onto a Chromium checkout (SRC=/path/to/chromium/src)
	@test -n "$(SRC)" || { echo "usage: make apply SRC=/path/to/chromium/src"; exit 2; }
	build/apply-patches.sh $(SRC)

bundle: ## Assemble the portable bundle (SRC=<out/Fortress> FONTS=<fonts dir> DEST=<dest>)
	@test -n "$(SRC)" && test -n "$(DEST)" || { echo "usage: make bundle SRC=<out dir> FONTS=fonts DEST=dist"; exit 2; }
	packaging/build-bundle.sh $(SRC) $(FONTS) $(DEST)

clean: ## Remove local build/test caches
	rm -rf .pytest_cache **/__pycache__ sdk/python/*.egg-info dist/*.tar.gz dist/SHA256SUMS
