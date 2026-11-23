# Installation

Installing Clamper puts a command line on your machine. Nothing happens in
your AWS account until you run `clamper deploy`, and `clamper demo` needs no
account at all.

Clamper is a Python package distributed through
[GitHub releases](https://github.com/jetonecloud/clamper/releases) rather
than PyPI. Python 3.11 or newer.

Pick a tag and stay on it. There is no `latest` that moves under you, which
is the point: for a tool that can stop your infrastructure, knowing exactly
what you are running is worth one extra token in the install command.

## pipx

The usual way. `pipx` keeps Clamper and its dependencies in their own
virtualenv rather than in your system Python.

```sh
pipx install git+https://github.com/jetonecloud/clamper@v0.1.0
clamper demo
```

To upgrade, install the new tag over the old one, then redeploy so the
enforcer runs the same version as your command line:

```sh
pipx install --force git+https://github.com/jetonecloud/clamper@v0.2.0
clamper deploy
```

## uv

```sh
uv tool install git+https://github.com/jetonecloud/clamper@v0.1.0
```

Or without installing anything permanently:

```sh
uvx --from git+https://github.com/jetonecloud/clamper@v0.1.0 clamper demo
```

## From the release wheel, without git

```sh
VERSION=0.1.0
BASE=https://github.com/jetonecloud/clamper/releases/download/v$VERSION

curl -fsSLO "$BASE/clamper-$VERSION-py3-none-any.whl"
pipx install "./clamper-$VERSION-py3-none-any.whl"
```

### Verifying what you downloaded

Every release carries a `checksums.txt` and a signed build provenance
attestation, which proves the wheel was built by the release workflow from
this repository rather than uploaded by hand. Worth the thirty seconds for a
tool you are about to give permission to stop things:

```sh
gh release download v0.1.0 --repo jetonecloud/clamper
sha256sum --check --ignore-missing checksums.txt
gh attestation verify clamper-0.1.0-py3-none-any.whl --repo jetonecloud/clamper
pipx install ./clamper-0.1.0-py3-none-any.whl
```

## Docker

```sh
docker run --rm ghcr.io/jetonecloud/clamper:0.1.0 demo

docker run --rm \
  -v "$PWD:/work:ro" \
  -v "$HOME/.aws:/home/clamper/.aws:ro" \
  -e AWS_PROFILE \
  ghcr.io/jetonecloud/clamper:0.1.0 \
  plan --config /work/clamper.yml
```

Both mounts are read-only: Clamper reads your config and your profile and
has no reason to write to either. The image runs as a non-root user with uid
1000. What it can do in your account is exactly what the credentials you
hand it allow, so for CI give it a read-only role (see below).

To write reports to the host, mount a directory at `/out` and pass
`--output-dir /out`.

## From a checkout

For development. See [`CONTRIBUTING.md`](../CONTRIBUTING.md).

```sh
git clone https://github.com/jetonecloud/clamper && cd clamper
uv sync --all-extras --dev
uv run clamper demo
```

## Credentials

Clamper never takes an access key on the command line, from a prompt or
from its config, and writes nothing about credentials to disk. It uses the
standard AWS chain — environment variables, `~/.aws/config`, SSO, a
container or instance role, `AWS_PROFILE` — and `--role-arn` assumes a role
from whatever that chain produced. Every command that touches AWS takes
`--profile`, `--region` and `--role-arn`.

Which permissions you need depends on what you are doing. All three policies
are in [`iam/`](../iam/README.md):

| Doing | Policy |
|---|---|
| `clamper deploy` | [`deploy-policy.json`](../iam/deploy-policy.json): broad, used once and on upgrades |
| `plan`, `status`, `ledger`, `doctor`, `simulate` | The read statements of the operator policy |
| `clamp`, `release` | The full [operator policy](../iam/operator-policy.example.json) |
| Nothing at all | `demo`, `init`, `validate`, `policy`, `deploy --template-only` |

`clamper policy --kind operator --account <id>` prints the operator policy
for your own config. Clamper warns if it is run with the account root
user's credentials, and `clamper doctor` flags it too.

## Deploying

```sh
clamper init --email you@example.com
clamper plan
clamper deploy
clamper doctor
```

`clamper deploy` asks before it changes anything, saying which account,
region and mode it is about to deploy, and then creates or updates two
CloudFormation stacks:

1. **`clamper-bootstrap`**: one S3 bucket for the enforcer's code.
   Versioned, encrypted, public access blocked, TLS required, old versions
   expired after 30 days, retained if the stack is deleted.
2. **`clamper`**: one budget per limit, the alerts and notifications topics,
   the ledger table, the enforcer function and its role, the six-hourly
   schedule, and a log group with 30-day retention.

Between the two it uploads the enforcer's zip under a key derived from its
contents, so redeploying an unchanged config uploads nothing and changes
nothing.

Then **confirm the email.** AWS sends a confirmation link to the address in
your config, and nothing is delivered until somebody clicks it. `clamper
doctor` fails until they have.

The stack lives in one region — `--region`, or your default. Budgets are
account-wide; the resources Clamper looks for are in the `regions` your
config lists.

### Deploying without giving Clamper the deploy permissions

```sh
clamper deploy --template-only --out build/
```

writes `bootstrap.yaml`, `main.yaml` and the enforcer zip to `build/`, makes
**no AWS calls at all**, and prints the three steps to deploy them yourself
from the console or your own pipeline. Read both templates first if you
like — they are plain CloudFormation with no macros or transforms — and run
`cfn-lint` over them.

### Upgrading and changing the config

Both are `clamper deploy` again. The config travels inside the zip, so a
change to `clamper.yml` does nothing in your account until it is deployed;
a new zip means a new key, and CloudFormation updates the function.
`clamper doctor` warns when the function running in AWS was not built from
the `clamper.yml` in front of it: it rebuilds the zip, which is
reproducible, and compares the digest with the function's. A different
Clamper or PyYAML version on the machine running `doctor` shows up the same
way, which is why it is a warning and not a failure.

`clamper doctor` also fails if the six-hourly reconcile schedule has been
disabled. Without it, only the Budgets alert — once per budget per period —
wakes the enforcer.

### Removing Clamper

Release anything still clamped first — `clamper status` shows it, and
`clamper release --all` puts it back. Then delete the `clamper` stack, and
then `clamper-bootstrap`, in the CloudFormation console.

Two things are retained on purpose and have to be deleted by hand if you
want them gone: the **ledger table**, because without it nothing Clamper
stopped can be released through Clamper; and the **artifact bucket**,
because CloudFormation cannot delete a bucket that still holds objects.
