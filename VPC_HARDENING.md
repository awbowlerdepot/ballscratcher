# VPC hardening: design note, not yet implemented

This documents the trade-off around replacing the RDS security group's
`0.0.0.0/0` inbound rule (a "quick unblock" from early in this project,
never tightened since) with real network-level restriction. Nothing in
this doc has been applied to `template.yaml` yet -- it's here so the
decision and its real cost can be made deliberately, not folded into
another change.

## Current state

- Every Lambda function in `template.yaml` runs **outside** a VPC (no
  `VpcConfig` on any function). Lambda's default (non-VPC) networking
  gives each invocation a public-internet-routable IP drawn from a large,
  shared, unpublished AWS pool -- it changes over time and per
  invocation, and AWS does not publish a stable CIDR range for it the way
  it does for, say, NAT Gateway Elastic IPs. That's *why* the RDS
  security group is currently wide open: there's no fixed IP or range to
  scope it to.
- Four functions (`UrlDiscoveryFunction`, `WooCommerceUrlDiscoveryFunction`,
  `NetsuiteUrlDiscoveryFunction`, `CommercebuildUrlDiscoveryFunction`)
  already have `VPCAccessPolicy: {}` in their `Policies` block. That's
  IAM permission only (`ec2:CreateNetworkInterface` etc.) -- with no
  `VpcConfig` set, it's currently unused/dead weight, not an actual
  network placement.
- Separately: a real DB password landed in plaintext in this chat earlier
  this session (from a `psql` connection-string paste). You said you
  weren't concerned about it at the time -- flagging again here only
  because it compounds with an open security group: a leaked password is
  a much smaller risk when the DB isn't reachable from the whole
  internet, and a much bigger one when it is. Worth rotating regardless
  of which option below you pick.

## Per-function classification

The reason this isn't a simple "just VPC-attach everything" fix: nearly
every function needs **both** private DB access **and** outbound internet
to scrape external manufacturer sites, and those two needs pull in
opposite directions once you're inside a VPC.

| Function | Needs DB | Needs outbound internet | Why |
|---|---|---|---|
| `UrlDiscoveryFunction` + WooCommerce/NetSuite/commercebuild equivalents (4 total) | Yes | Yes | Write `discovered_urls`; fetch sitemaps/category listings |
| `ProductScraperFunction` + WooCommerce/NetSuite/commercebuild equivalents (4 total) | Yes | Yes | Write products; fetch product pages |
| `PdfParserFunction` | Yes | Yes | Fetch PDF bytes from `info_sheet_url` |
| `ImageProcessorFunction` | Yes | Yes | Fetch source images from manufacturer CDNs |
| `BowwwlCrossCheckFunction` | Yes | Yes | Fetch bowwwl.com pages |
| `BowlerDepotReconciliationFunction` | Yes | Yes | Call BigCommerce's API |
| `AdminApiFunction` | Yes | **No** | Serves API Gateway requests + reads/writes Postgres only -- inbound invocation via API Gateway doesn't require outbound internet |
| `AdminApiAuthorizerFunction` | **No** | **No** | Only reads a Secrets Manager token -- doesn't touch Postgres at all |

13 of 15 functions need both. Only `AdminApiFunction` needs the DB
without needing outbound internet, and `AdminApiAuthorizerFunction` needs
neither.

## Options

### Option A: Full VPC, all DB-touching functions, with a NAT Gateway

Put all 14 DB-touching functions in private subnets. A NAT Gateway
provides the outbound internet path the 13 scraper/QA functions still
need; RDS's security group is then locked down to only the Lambdas'
security group, and `0.0.0.0/0` goes away entirely.

**Real cost, us-west-1 pricing as of this session:**
- NAT Gateway: ~$0.045/hour ≈ **$32-33/month** just for it to exist, plus
  ~$0.045/GB processed. This project's traffic volume is small (scraping
  product pages, not bulk transfer), so the per-GB charge is likely a few
  dollars/month on top, not the dominant cost.
- For actual multi-AZ availability you'd want a NAT Gateway per AZ
  (~$64-66/month for two) -- a single NAT Gateway in one AZ is cheaper
  but becomes a single point of failure for every scraper function if
  that AZ has an outage. For a project at this scale, one NAT Gateway is
  a defensible trade-off; just be aware it's the corner being cut.
- Two new private subnets (if they don't already exist in whatever
  VPC/subnets RDS lives in) + the NAT Gateway's own subnet routing --
  no direct cost themselves, but real setup work if this VPC doesn't
  already have that subnet layout.

**This is the only option that actually closes `0.0.0.0/0` completely.**

### Option B: VPC only for `AdminApiFunction` (no NAT Gateway needed)

`AdminApiFunction` doesn't need outbound internet, so it can move into a
private subnet with zero NAT cost. `AdminApiAuthorizerFunction` doesn't
touch the DB at all, so it has no reason to move either way.

**Honest assessment: this barely moves the needle.** The other 13
functions -- the actual bulk of what talks to Postgres -- still need
unpredictable-source-IP internet access, so the RDS security group still
needs to accept connections from *something* outside the VPC for them.
There's no valid narrower CIDR to scope that to (see "Current state"
above -- non-VPC Lambda doesn't have a stable IP range to allowlist,
unlike what I initially suggested when asking about this trade-off). In
practice Option B removes one function's exposure but leaves the
substantive risk -- a Postgres port reachable from anywhere -- unchanged.

### Option C: Don't VPC-attach anything; compensating controls instead

If the NAT Gateway cost isn't worth it at this project's scale, the
security group stays open, but the actual risk can still be reduced:

- **Rotate the DB password** that leaked in this session's chat history
  (flagged above) -- independent of everything else here, this is cheap
  and should happen regardless.
- **Enforce TLS-only connections** on the RDS instance (`rds.force_ssl =
  1` in the parameter group) so credentials and query data aren't sent
  in the clear even over the open security group.
- **IAM database authentication** instead of (or alongside) a static
  password -- short-lived tokens instead of a long-lived credential
  reduce how much damage a leaked secret does.
- **CloudWatch/RDS connection logging + alerting** on connections from
  unexpected source IPs, as a detective control since a preventive
  network boundary isn't in place.

This doesn't eliminate the exposure the way Option A does, but it's real
risk reduction with no ongoing AWS cost, and might be the right call if
this project's traffic/budget doesn't justify $32+/month for a NAT
Gateway.

## Recommendation

Option A if the $32-45/month is acceptable -- it's the only one that
actually closes the hole rather than narrowing or compensating for it.
Option C is a reasonable interim step regardless of which way you land
long-term (password rotation and TLS enforcement cost nothing and help
either way). Option B isn't recommended on its own -- the cost savings
over Option A don't justify how little risk it actually removes.

## If you decide to move forward

Not written yet, pending a decision above. Would involve: VPC/subnet/
security-group parameters added to `template.yaml` (reusing an existing
VPC if RDS already lives in one, rather than provisioning a new one from
this template), `VpcConfig` added to the 14 DB-touching functions'
`AWS::Serverless::Function` resources, a new Lambda-specific security
group scoped to outbound-only + RDS access, tightening the RDS security
group to that new SG instead of `0.0.0.0/0`, and (for Option A) the NAT
Gateway + its route table entries. Flag when you're ready to pick one and
this can be scoped out properly.
