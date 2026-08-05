.PHONY: dev test test-fast lint format typecheck webui-dev webui-build webui-test webui-lint install check

# 安装开发依赖
install:
	uv pip install -e ".[dev]"
	cd webui && bun install

# 启动开发服务器
dev:
	uv run biscuitbot run

# WebUI 开发
webui-dev:
	cd webui && bun run dev

webui-build:
	cd webui && bun run build

webui-test:
	cd webui && bun run test

webui-lint:
	cd webui && bun run lint

# 测试
test:
	uv run pytest tests/

test-fast:
	uv run pytest tests/ -x -q

# 代码检查
lint:
	uv run ruff check biscuitbot
	uv run ruff format --check biscuitbot

format:
	uv run ruff check biscuitbot --fix
	uv run ruff format biscuitbot

# 类型检查
typecheck:
	uv run pyright biscuitbot

# 全量检查（提交前运行）
check: lint test
