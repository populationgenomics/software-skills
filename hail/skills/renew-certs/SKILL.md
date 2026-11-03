---
name: renew-certs
description: Renew the Let's Encrypt TLS certificates for the CPG Hail deployment on GCP. Starts the hail-dev VM, runs cert_renewal.sh on it as you, checks the new expiry date, and stops the VM again.
disable-model-invocation: true
---

You are renewing the CPG Hail TLS certificates (team-docs: hail.md, "Updating TLS (HTTPS)
certificates"). The work happens on the `hail-dev` VM, which costs money while running and is
shared, so leave it in the state you found it. Never delete the VM. Never touch `hail-dev22-old`
or any other VM.

Find the zone first, rather than hardcoding it:
```
gcloud compute instances list --project=hail-295901 --filter='name=hail-dev' --format='value(zone.basename())'
```
Call the result `<zone>`. If it is empty, stop: the VM is gone or has been renamed. As of Nov 2026
the VM was in `australia-southeast1-c`. team-docs said `-b`, which is the GKE cluster's zone.

Write `--zone=<zone> --project=hail-295901` out literally in every gcloud command. Don't put the
flags in a shell variable: the user's shell may be zsh, which doesn't split `$VAR` into words.

Expiry check (used twice):
```
echo | openssl s_client -servername batch.hail.populationgenomics.org.au -connect batch.hail.populationgenomics.org.au:443 2>/dev/null | openssl x509 -noout -enddate
```

## 1. Preflight: stop at the first failure and say why
- `gcloud auth list --filter=status:ACTIVE --format='value(account)'` must print an account.
  If not, tell the user to run `! gcloud auth login`.
- Run the expiry check. Report the date and the days remaining. Let's Encrypt certs last 90 days,
  so more than 30 days left means this run is probably early. Ask before continuing in that case.
- `gcloud compute instances describe hail-dev --zone=<zone> --project=hail-295901 --format='value(status)'`.
  Remember it as `<initial>`. If it is `RUNNING`, someone may be using the VM. Say so.

## 2. Confirm
The script restarts every Hail service pod (`kubectl delete pods`). That causes a very short
outage but doesn't interrupt running batches. Get an explicit yes before going further.

## 3. Start the VM
If `<initial>` is not `RUNNING`: `gcloud compute instances start hail-dev --zone=<zone> --project=hail-295901`.
From here on, step 6 must run even if something fails.

## 4. Docker group
```
gcloud compute ssh hail-dev --zone=<zone> --project=hail-295901 --quiet --command 'id -nG'
```
The first ssh after boot usually fails with exit code 255 because sshd isn't up yet. Re-run the same
command; don't use `sleep` loops.

OS Login is on, so the VM username comes from the Google account, cut to 32 characters (e.g.
`jane_doe_populationgenomics_org`), not the local username. That is expected. The first login
creates the home directory.

If the groups lack `docker`, ask, then run `--command 'sudo usermod -aG docker $USER'` and repeat
the `id -nG` check. Each ssh is a new login, so the change shows up straight away.

## 5. Run the renewal script
```
gcloud compute ssh hail-dev --zone=<zone> --project=hail-295901 --quiet --command \
  'curl -sSL https://raw.githubusercontent.com/populationgenomics/hail/main/cert_renewal.sh | bash' \
  > /tmp/cert_renewal.log 2>&1; echo "exit=$?"
```
Use a 10-minute Bash timeout. `make run` takes a few minutes. The log opens with about 40 KB of
ANSI art, so don't print it whole. Strip it, then show the tail:
`grep -av $'\e\\[38;2' /tmp/cert_renewal.log | sed 's/\x1b\[[0-9;]*m//g' | grep -v '^\s*$' | tail -60`.
Summarise the certbot result ("This certificate expires on …"), the restarted pods, and the
final `notAfter=`. If it fails, show the failing stage's output and ask whether to debug on the VM
before stopping it.

## 6. Stop the VM
If `<initial>` was not `RUNNING`: `gcloud compute instances stop hail-dev --zone=<zone> --project=hail-295901`.
If it was already running, ask whether to stop it.

## 7. Verify and report
Run the expiry check locally. The new `notAfter` should be about 90 days out. If it hasn't
changed, the pods may still be restarting. Check once more in a minute. Also check that
`curl -s -o /dev/null -w '%{http_code}' https://batch.hail.populationgenomics.org.au/healthcheck`
returns 200.
Report the old and new expiry dates, the health check and the VM's final state. Remind the user
to schedule a reminder about 2 weeks before the new expiry. If the current month is January or
February, also mention that the internal `hail-root` certificate is due for its annual
regeneration (team-docs: "Regenerating the `hail-root` certificate"). This skill doesn't do that.
