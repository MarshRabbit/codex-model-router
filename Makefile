.PHONY: test check
test:
	python3 -m unittest discover -s tests -v
check: test
	PYTHONPYCACHEPREFIX=./work/pycache python3 -m compileall -q skills/codex-model-router/scripts router.py
