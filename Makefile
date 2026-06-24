PORT     := 8090   # ADK dev playground
SRV_PORT := 8080   # ambient Pub/Sub service

.PHONY: help install playground stop serve

help:
	@grep -E '^[a-z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Install all project dependencies
	uv sync

stop: ## Kill any process already bound to PORT (default 8090)
	-fuser -k $(PORT)/tcp 2>/dev/null; true

playground: stop ## Start the ADK dev UI at http://127.0.0.1:$(PORT)/dev-ui/?app=app
	agents-cli playground --port $(PORT)

serve: ## Run the ambient Pub/Sub web service on SRV_PORT (default 8080)
	uv run uvicorn app.fast_api_app:web_app \
	  --host 0.0.0.0 --port $(SRV_PORT) --reload
