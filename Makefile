.PHONY: up down logs test demo lint clean

up:
	docker compose up -d --build

down:
	docker compose down -v

logs:
	docker compose logs -f --tail=100

test:
	python3 -m pytest -q

demo:
	./scripts/demo.sh

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache
