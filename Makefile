# One-click entry points. plan.md week 15 asks for smoke, experiment,
# aggregation and plotting to be reachable without reading the scripts,
# and its acceptance clause is that a clean machine can run the small
# artifact smoke test in one step.
#
# Targets are split by what they need. `smoke`, `test` and the analysis
# targets need only Python and the repo. Everything under "on the card"
# needs ROCm, a gfx1201, and an idle GPU -- they refuse to start on top
# of a running cell rather than queueing behind it.

PY ?= python3
PYTHONPATH := src
export PYTHONPATH

GRID := experiments/runs/matrix_20260809 experiments/runs/matrix_cover
EXPA := experiments/runs/expA

.PHONY: help smoke test grid expA expA-run campaign-gaps vram paper

help:
	@echo "no GPU needed:"
	@echo "  make smoke        simulate every policy, check determinism and wiring (<1 s)"
	@echo "  make test         the unit suite"
	@echo "  make grid         per-policy means over the 405-cell grid, and"
	@echo "                    how many configurations could separate the policies"
	@echo "  make expA         judge experiment A by its pre-registered verdicts"
	@echo "  make paper        build paper/burstserve.pdf"
	@echo ""
	@echo "on the card (gfx1201, refuses to start if one is already running):"
	@echo "  make expA-run     experiment A: static split vs the scheduler (~7 h)"
	@echo "  make campaign-gaps  the reviewer-gap campaigns (~12 h)"
	@echo "  make vram         peak VRAM per residency regime (~5 min)"

smoke:
	$(PY) scripts/smoke.py

test:
	$(PY) -m unittest discover -s tests -q

grid:
	$(PY) scripts/summarise_grid.py $(GRID)

expA:
	$(PY) scripts/analyse_experiment_a.py $(EXPA) --json $(EXPA)/verdict.json

expA-run:
	bash scripts/campaign_experiment_a.sh

campaign-gaps:
	bash scripts/campaign_reviewer_gaps.sh

vram:
	$(PY) scripts/measure_vram_budget.py \
	  --out experiments/runs/vram_budget/r9700.json

# latexmk, not a paper/Makefile: paper/README.md records that
# `kpsewhich acmart.cls` reported success while a sigconf document could
# not build, so the only check that counts here is a compile.
paper:
	cd paper && latexmk -pdf burstserve.tex
