# IndexNow notifications

The owner authorized this notification job on 21 September 2026. It uses the existing
public `nickbasharin/aifra-uptime` repository, no paid services or credentials. The root
verification text file and `/indexnow.json` are deliberately public ownership proofs,
not administrative secrets. They never confer upload or account access.

`infra/scripts/indexnow.ts` is the source; the public job uses its standalone Node.js
ESM build. `infra/indexnow-workflow.yml` is the workflow source. Build with:

```sh
pnpm exec esbuild infra/scripts/indexnow.ts --bundle --platform=node --format=esm --outfile=indexnow.mjs
```

Set `SITE_ORIGIN` to the reviewed HTTPS platform origin. Without `--submit`, the script
only checks pages. With `--submit`, it sends new/changed HTML from the sitemap to
`https://api.indexnow.org/indexnow`. It refuses foreign origins, preview/API/admin
routes, redirects, noncanonical pages and noindex headers/meta. Machine files, video and
images are not submitted. It does not notify removed URLs.

The workflow runs daily and can be dispatched manually. Successful state is cached
between runs; a failure leaves the previous state intact for the next retry. Expired
GitHub cache can result in a full resubmission, not loss of pages. Concurrency prevents
overlapping state writes. A 200 means receipt, a 202 means key validation pending;
neither proves indexing, ranking or recommendations by AI systems. Google Search Console
remains a separate channel.

Rollback: disable the workflow or revert its commit. Public proof files may remain.
Previously sent notifications cannot be recalled. The service runtime, uploaded content,
preview noindex, DNS and TLS do not depend on this job.
