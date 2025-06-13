# Inspired by: https://blog.mathieu-leplatre.info/tips-for-your-makefile-with-python.html

PYMODULE := networking_unifi
TESTS := tests
INSTALL_STAMP := .install.stamp
POETRY := $(shell command -v poetry 2> /dev/null)
MYPY := $(shell command -v mypy 2> /dev/null)

.DEFAULT_GOAL := help

.PHONY: all
all: install lint test

.PHONY: help install dev-install clean lint format test test-cov test-macos pre-commit update

# Display help information
help:
	@echo "UniFi ML2 Driver Make Commands"
	@echo ""
	@echo "Usage: make [command]"
	@echo ""
	@echo "Commands:"
	@echo "  install          Install the package and dependencies"
	@echo "  dev-install      Install the package and development dependencies"
	@echo "  clean            Remove build artifacts"
	@echo "  lint             Run linting (flake8, mypy)"
	@echo "  format           Format code (black, isort)"
	@echo "  test             Run tests"
	@echo "  test-cov         Run tests with coverage"
	@echo "  test-macos       Run tests compatible with macOS (skips Linux-specific tests)"
	@echo "  pre-commit       Run pre-commit checks on all files"
	@echo "  update           Update dependencies"

# Install the package
install:
	poetry install --no-dev

# Install the package with development dependencies
dev-install:
	poetry install

# Clean build artifacts
clean:
	rm -rf build/
	rm -rf dist/
	rm -rf *.egg-info
	rm -rf .pytest_cache/
	rm -rf .coverage
	rm -rf htmlcov/
	rm -rf coverage.xml
	find . -type d -name __pycache__ -exec rm -rf {} +

# Run linting
lint:
	poetry run flake8 networking_unifi
	poetry run mypy networking_unifi

# Format code
format:
	poetry run black networking_unifi
	poetry run isort networking_unifi

# Run tests
test:
	poetry run pytest

# Run tests with coverage
test-cov:
	poetry run pytest --cov=networking_unifi --cov-report=term-missing --cov-report=xml:coverage.xml --cov-report=html:htmlcov

# Run tests compatible with macOS (skips Linux-specific tests)
test-macos:
	poetry run pytest networking_unifi/tests/unit/test_exceptions.py -v

# Run pre-commit checks
pre-commit:
	poetry run pre-commit run --all-files

# Update dependencies
update:
	poetry update
