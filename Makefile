PYTHON := .venv/bin/python
FRONT  := davinci-frontend

.PHONY: gen-types
## gen-types: regenera o contrato de tipos Django -> front (OpenAPI -> TS).
## Fluxo: mudou serializer/view no Django -> roda `make gen-types` -> `tsc` acusa qualquer divergencia.
gen-types:
	$(PYTHON) manage.py spectacular --file $(FRONT)/schema.yml --fail-on-warn
	cd $(FRONT) && npm run gen:types
	@echo "Tipos regenerados. Rode 'cd $(FRONT) && npx tsc --noEmit' para validar o contrato."

.PHONY: help
help:
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/## //'
