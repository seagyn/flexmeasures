# Note: use tabs
# actions which are virtual, i.e. not a script
.PHONY: install install-for-dev install-for-test install-deps install-flexmeasures run-local test freeze-deps upgrade-deps update-docs update-docs-pdf show-file-space show-data-model clean-db


# ---- Development ---

run-local:
	python run-local.py

test:
	make install-for-test
	pytest

# ---- Documentation ---

gen_code_docs := False # by default code documentation is not generated

update-docs:
	@echo "Creating docs environment ..."
	make install-docs-dependencies
	@echo "Creating documentation ..."
	export GEN_CODE_DOCS=${gen_code_docs}; cd documentation; make clean; make html SPHINXOPTS="-W --keep-going -n"; cd ..

update-docs-pdf:
	@echo "NOTE: PDF documentation requires packages (on Debian: latexmk texlive-latex-recommended texlive-latex-extra texlive-fonts-recommended)"
	@echo "NOTE: Currently, the docs require some pictures which are not in the git repo atm. Ask the devs."
	make install-sphinx-tools

	export GEN_CODE_DOCS=${gen_code_docs}; cd documentation; make clean; make latexpdf; make latexpdf; cd ..  # make latexpdf can require two passes

# ---- Installation ---

install: install-deps install-flexmeasures

install-for-dev:
	make freeze-deps
	pip-sync requirements/app.txt requirements/dev.txt requirements/test.txt
	make install-flexmeasures

install-for-test:
	make install-pip-tools
# Pass pinned=no if you want to test against latest stable packages, default is our pinned dependency set
ifneq ($(pinned), no)
	pip-sync requirements/app.txt requirements/test.txt
else
	# cutting off the -c inter-layer dependency (that's pip-tools specific)
	tail -n +3 requirements/test.in >> temp-test.in
	pip install --upgrade -r requirements/app.in -r temp-test.in
	rm temp-test.in
endif
	make install-flexmeasures

install-deps:
	make install-pip-tools
	make freeze-deps
# Pass pinned=no if you want to test against latest stable packages, default is our pinned dependency set
ifneq ($(pinned), no)
	pip-sync requirements/app.txt
else
	pip install --upgrade -r requirements/app.in
endif

install-flexmeasures:
	pip install -e .

install-pip-tools:
	pip3 install -q "pip-tools>=7.0"

install-docs-dependencies:
	pip install -r requirements/docs.txt

freeze-deps:
	make install-pip-tools
	pip-compile -o requirements/app.txt requirements/app.in
	pip-compile -o requirements/test.txt requirements/test.in
	pip-compile -o requirements/dev.txt requirements/dev.in
	pip-compile -o requirements/docs.txt requirements/docs.in

upgrade-deps:
	make install-pip-tools
	pip-compile --upgrade -o requirements/app.txt requirements/app.in
	pip-compile --upgrade -o requirements/test.txt requirements/test.in
	pip-compile --upgrade -o requirements/dev.txt requirements/dev.in
	pip-compile --upgrade -o requirements/docs.txt requirements/docs.in
	make test


# ---- Data ----

show-file-space:
	# Where is our file space going?
	du --summarize --human-readable --total ./* ./.[a-zA-Z]* | sort -h

upgrade-db:
	flask db current
	flask db upgrade
	flask db current

show-data-model:
	# This generates the data model, as currently written in code, as a PNG picture.
	# Also try with --schema for the database model. 
	# With --deprecated, you'll see the legacy models, and not their replacements.
	# Use --help to learn more. 
	./flexmeasures/data/scripts/visualize_data_model.py --uml

clean-db:
	./flexmeasures/data/scripts/clean_database.sh ${db_name} ${db_user}
