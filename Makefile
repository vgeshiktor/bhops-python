# Define the environment name
ENV_NAME := py3.12

# Define requirements files
REQUIREMENTS := requirements.txt
DEV_REQUIREMENTS := requirements-dev.txt

# Use conda environment to run the tools
CONDA_RUN := conda run -n $(ENV_NAME)

# Create a Conda environment
create_env:
	conda create -y -n $(ENV_NAME) python=3.11

# Install dependencies
install: create_env
	$(CONDA_RUN) pip install -r $(REQUIREMENTS)

# Install development dependencies
install-dev: create_env
	$(CONDA_RUN) pip install -r $(REQUIREMENTS) -r $(DEV_REQUIREMENTS)

# Run tests using pytest
test:
	$(CONDA_RUN) pytest

# Run code formatting with black
format:
	$(CONDA_RUN) black .

# Run linting with flake8
lint:
	$(CONDA_RUN) flake8 .

# Clean up cache files and remove the Conda environment
clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	rm -rf .pytest_cache
	conda env remove -n $(ENV_NAME)

# Run all checks (formatting, linting, and testing)
check: format lint test
