# Collaboration release site

Static HTML/CSS/JavaScript. No generated backend, account access, private data, credentials or paid dependencies. Current live apps remain separate; combined release is pending.

## Proposed Pages publication

After cross-side review, merge this PR with a merge commit. Repository owner selects Settings > Pages > Build and deployment > Source: GitHub Actions. Run the manual `Publish collaboration release site` workflow from `main` only after approval of the actual page. It uploads only `site/`, not either source package. No automatic publication occurs on PR creation or merge. No custom domain, live-app migration, DNS change or service cutover.

GitHub Actions/Pages use free public-repository hosting; no paid plan or payment method needed. Repository settings permissions are required and are not inferred from collaborator WRITE.

## Content maintenance

Public capability descriptions stay beside acceptance caveats. Update dates and proof before changing any status. Counterpart-reported capabilities are labelled as reports. Existing source/feature history is not a universal real-device or provider acceptance claim.

## Preview

Open `site/index.html` or serve `site/` with a simple static server. Inspect320px,390px and1280px widths, capability filters, keyboard focus, section links, reduced motion and all external destinations. No build step.
