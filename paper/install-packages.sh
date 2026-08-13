#!/usr/bin/env bash
# Install what this draft's candidate submission classes need.
#
# TeX Live 2026 lives at /data/zhuoxu/tools/texlive/2026 and is writable
# by the running user, so none of this needs sudo.
#
# Run it in the background or under a long timeout. tlmgr fetches the
# whole package database on first use, which takes minutes; two attempts
# here were killed under a 60-120 s timeout and left acmart marked
# installed with its dependencies missing, which is how the totpages and
# environ failures below arose.
set -u

# Refuse to run beside another tlmgr. Two of them race on
# tlpkg/texlive.tlpdb.tmp -- one renames it away while the other still
# expects it -- and the loser dies with
#   lstat(.../texlive.tlpdb.tmp) failed: No such file or directory
# leaving packages half-installed. That is how acmart ended up marked
# present with totpages and environ missing. The real risk is a
# corrupted texlive.tlpdb, which costs far more than waiting.
if pgrep -f "[t]lmgr" | grep -qv "^$$\\?$"; then
  echo "tlmgr is already running:" >&2
  pgrep -af "[t]lmgr" | sed "s/^/  /" >&2
  echo "Wait for it, or stop it, then re-run. Do not run two." >&2
  exit 1
fi

# collection-latexextra is about 2000 packages and takes over two hours
# on this link. Run it detached rather than under a timeout.

# 1. The two that install cleanly on their own and are verified to
#    compile a minimal document.
tlmgr install siunitx        # verified: \SI renders
tlmgr install ieeetran       # NOTE: lowercase. "IEEEtran" is not a
                             # package name and tlmgr reports
                             # "not present in repository".
                             # verified: \documentclass[conference]{IEEEtran} builds

# 2. acmart. Installing the class alone is not enough and installing its
#    dependencies one at a time is whack-a-mole -- it failed first on
#    totpages.sty, then on environ.sty. Install the collection instead.
tlmgr install collection-latexextra collection-fontsrecommended

# 3. Verify by compiling, not by looking. `kpsewhich acmart.cls`
#    reported success while a minimal sigconf document could not build.
cd "$(mktemp -d)" || exit 1
cat > t.tex <<'TEX'
\documentclass[sigconf,nonacm]{acmart}
\begin{document}\title{t}\author{a}\affiliation{\institution{i}}\maketitle ok\end{document}
TEX
pdflatex -interaction=nonstopmode t.tex >/dev/null 2>&1
if [ -f t.pdf ]; then echo "acmart: builds"; else echo "acmart: still broken"; grep -m1 '^!' t.log; fi

# llncs is not on CTAN; Springer ships it with their author kit. If a
# venue needs it, fetch it from the venue and drop llncs.cls beside the
# .tex file -- there is nothing to install.
