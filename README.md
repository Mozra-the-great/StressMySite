# Repo Template

Standard-Vorlage für neue Repositories. Enthält die Grundausstattung, die jedes
neue Projekt sowieso bekommen soll, damit sie nicht jedes Mal manuell
nachgezogen werden muss.

## Verwendung

1. Auf GitHub bei einem neuen Repo **"Repository template"** auf dieses Repo
   setzen (Dropdown beim Erstellen, oder Button "Use this template" auf dieser
   Repo-Seite). GitHub kopiert dann Dateien und Struktur, aber **nicht**
   Branch-Protection, Secrets oder Repo-Settings — die müssen pro neuem Repo
   einmal nachgezogen werden (siehe unten).
2. `CLAUDE.md` oben ausfüllen (`<PROJECT_NAME>`, Type/Stack/Structure/Commands/
   Notes) — der untere Abschnitt "Working rules" bleibt unverändert, der
   spiegelt die globale Claude-Code-Konfiguration.
3. `NOTICE` und `README.md` an das eigentliche Projekt anpassen.
4. Falls das Projekt die Claude-Code-GitHub-Actions nutzen soll (`@claude` in
   Issues/PRs, automatisches PR-Review): Secret setzen —
   `gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo <owner>/<neues-repo>`
   (der Wert ist der gleiche, der bereits bei `nagellacke` hinterlegt ist).
5. Sobald echter Code im Repo liegt: CodeQL Default-Setup aktivieren
   (Settings → Code security → Code scanning → Set up → Default), danach
   Branch Protection auf `main` einrichten, analog zu `nagellacke`:
   ```sh
   gh api repos/<owner>/<repo>/branches/main/protection -X PUT --input - <<'EOF'
   {
     "required_status_checks": {
       "strict": false,
       "contexts": ["CodeQL", "claude-review"]
     },
     "enforce_admins": true,
     "required_pull_request_reviews": null,
     "restrictions": null,
     "allow_force_pushes": false,
     "allow_deletions": false
   }
   EOF
   ```
   Die genauen Check-Namen in `contexts` hängen von den tatsächlich
   aktivierten Workflows/Sprachen ab — mit `gh api repos/<owner>/<repo>/commits/main/check-runs`
   nachsehen, sobald der erste Lauf durch ist.

## Was hier drin ist

| Datei | Zweck |
|---|---|
| `CLAUDE.md` | Arbeitsregeln für Claude Code (Git-Workflow, Security, Subagenten) + Platzhalter für Projekt-Kontext |
| `.claude/settings.json` | Permissions, die Claude Code in diesem Repo automatisch erlaubt sind |
| `LICENSE` / `NOTICE` | Apache License 2.0 |
| `.gitignore` / `.editorconfig` | generische Basis-Regeln |
| `CONTRIBUTING.md` | Branch-Namen, Commit-Konvention, PR-Ablauf |
| `SECURITY.md` | Meldeweg für Sicherheitslücken |
| `CHANGELOG.md` | Keep-a-Changelog-Format, startet leer |
| `.github/ISSUE_TEMPLATE/` | Bug-Report + Feature-Request |
| `.github/PULL_REQUEST_TEMPLATE.md` | PR-Checkliste |
| `.github/workflows/claude.yml` | `@claude` in Issues/PR-Kommentaren reagiert |
| `.github/workflows/claude-code-review.yml` | automatisches Claude-Code-Review auf jeden PR |

## Lizenz

Apache License 2.0 — siehe [`LICENSE`](LICENSE). Gilt für alle Projekte, die aus
dieser Vorlage entstehen, sofern im jeweiligen Repo nicht ausdrücklich anders
vermerkt.
