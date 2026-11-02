# Agent GitHub Account Setup Guide: Separate Identity & Attribution (`onasafari`)

This guide explains how to set up a dedicated GitHub account or persona (such as **`onasafari`**) for AI coding agents (Antigravity Agent, CI bots, automated scripts) distinct from your primary personal GitHub account (**`arii`**).

It covers two architectural approaches:
1. **Attribution-Only Mode (Recommended & Zero-Friction):** Code commits, signatures, and avatars display as the agent (`onasafari` / `Antigravity Agent`) with verified badges, while push access seamlessly uses your existing developer credentials.
2. **Full Dual-Account Mode:** The agent has separate SSH keys, separate GitHub CLI (`gh`) login sessions, and independent repository collaborator access.

---

## Table of Contents
1. [Core Concepts: Attribution vs. Authentication](#1-core-concepts-attribution-vs-authentication)
2. [Method 1: Commit Attribution & GPG Verification (Zero-Friction)](#2-method-1-commit-attribution--gpg-verification-zero-friction)
3. [Method 2: Full Dual-Account Authentication (Separate SSH Keys)](#3-method-2-full-dual-account-authentication-separate-ssh-keys)
4. [Method 3: Directory-Level Configuration via `git includeIf`](#4-method-3-directory-level-configuration-via-git-includeif)
5. [Method 4: Multi-Account Management with GitHub CLI (`gh`)](#5-method-4-multi-account-management-with-github-cli-gh)
6. [Method 5: GitHub App / Bot Identity (`[bot]`)](#6-method-5-github-app--bot-identity-bot)
7. [Verification & Diagnostic Checklist](#7-verification--diagnostic-checklist)

---

## 1. Core Concepts: Attribution vs. Authentication

When working with GitHub, two distinct mechanisms determine who is doing what:

| Concept | What It Controls | What Determines It |
| :--- | :--- | :--- |
| **Commit Attribution** | Whose avatar, name, and profile link appear on commits and pull request diffs | The commit's `user.email` matching an email verified on a GitHub profile, plus GPG signing key |
| **Push / API Authentication** | Whether Git or GitHub API permits pushing branches, opening PRs, or modifying issues | SSH private key (`~/.ssh/id_rsa`), personal access token (`gh auth`), or GitHub App installation |

> [!TIP]
> In this workspace, commits are authored by **`Antigravity Agent <ariandersrobotics@gmail.com>`** and signed by **`onasafari`'s GPG key**. Because `ariandersrobotics@gmail.com` is registered on the GitHub user `onasafari`, GitHub automatically renders all agent commits as `onasafari` with a **Verified** badge—even when pushing through SSH authenticated as `arii`.

---

## 2. Method 1: Commit Attribution & GPG Verification (Zero-Friction)

This is the cleanest and fastest setup. It requires no SSH host multiplexing or multi-account repository permission management.

### Step 1: Register the Agent Email on GitHub
1. Log into the agent account on GitHub ([`github.com/onasafari`](https://github.com/onasafari)).
2. Go to **Settings** → **Emails** (`https://github.com/settings/emails`).
3. Add the agent email address (e.g., `ariandersrobotics@gmail.com`).
4. Click the verification link sent to that inbox.

### Step 2: Configure Git User Info
Set the agent name and email in your workspace or global config:

```bash
# Set globally:
git config --global user.name "Antigravity Agent"
git config --global user.email "ariandersrobotics@gmail.com"

# Or set only in specific repository:
git config user.name "Antigravity Agent"
git config user.email "ariandersrobotics@gmail.com"
```

### Step 3: Generate a GPG Signing Key for the Agent
To get the green **"Verified"** badge next to agent commits on GitHub:

```bash
# Generate a new 4096-bit RSA key
gpg --full-generate-key
```
- Select `(1) RSA and RSA`
- Keysize: `4096`
- Expiration: `2y` (or desired duration)
- Real name: `onasafari`
- Email address: `ariandersrobotics@gmail.com` (or `anders.ariel@gmail.com`)
- Set a passphrase (or leave empty if used in fully automated CI/agent workflows).

### Step 4: Add GPG Key to GitHub & Configure Git
List your GPG keys to get the Key ID:

```bash
gpg --list-secret-keys --keyid-format=long
```

Output example:
```text
sec   rsa4096/BBB26C2C8E2C66BD 2025-11-30 [SC] [expires: 2027-11-30]
      BBB26C2C8E2C66BDD1C83621F33835F96989A742
uid                 [ultimate] onasafari <anders.ariel@gmail.com>
```

Export the public key in ASCII armor format:
```bash
gpg --armor --export BBB26C2C8E2C66BDD1C83621F33835F96989A742
```

1. Copy the entire output block starting with `-----BEGIN PGP PUBLIC KEY BLOCK-----`.
2. On GitHub (logged into `onasafari`), go to **Settings** → **SSH and GPG keys** (`https://github.com/settings/keys`).
3. Click **New GPG key**, paste the block, and click **Add GPG key**.

Finally, enable automatic signing in Git:
```bash
git config --global user.signingkey BBB26C2C8E2C66BDD1C83621F33835F96989A742
git config --global commit.gpgsign true
git config --global tag.gpgsign true
```

---

## 3. Method 2: Full Dual-Account Authentication (Separate SSH Keys)

If you need the agent account to also have separate **push credentials** (so GitHub API logs show operations originating from `onasafari` directly):

### Step 1: Generate a Dedicated SSH Key
```bash
ssh-keygen -t ed25519 -C "ariandersrobotics@gmail.com" -f ~/.ssh/id_ed25519_onasafari
```

### Step 2: Add SSH Key to Agent Account
1. Copy the public key:
   ```bash
   cat ~/.ssh/id_ed25519_onasafari.pub
   ```
2. Log into `github.com/onasafari` → **Settings** → **SSH and GPG keys** → **New SSH key**.

### Step 3: Grant Repository Permissions
From your primary account (`github.com/arii`):
1. Go to target repository (e.g., `https://github.com/arii/portfolio/settings/access`).
2. Click **Add people** → Invite `onasafari` with **Write** or **Admin** role.
3. Accept the invite from `onasafari`'s email or at `https://github.com/arii/portfolio/invitations`.

### Step 4: Configure `~/.ssh/config` Host Aliases
Open or create `~/.ssh/config`:

```ssh-config
# Primary personal account (arii)
Host github.com
  HostName github.com
  User git
  IdentityFile ~/.ssh/id_rsa
  IdentitiesOnly yes

# Dedicated agent account (onasafari)
Host github.com-onasafari
  HostName github.com
  User git
  IdentityFile ~/.ssh/id_ed25519_onasafari
  IdentitiesOnly yes
```

Set file permissions:
```bash
chmod 600 ~/.ssh/config ~/.ssh/id_ed25519_onasafari
```

### Step 5: Test the Connections
```bash
# Test primary connection (should say "Hi arii!")
ssh -T git@github.com

# Test agent connection (should say "Hi onasafari!")
ssh -T git@github.com-onasafari
```

### Step 6: Clone or Update Remotes for Agent Repos
To push as `onasafari`, point the remote to the alias:
```bash
git remote set-url origin git@github.com-onasafari:arii/portfolio.git
```

---

## 4. Method 3: Directory-Level Configuration via `git includeIf`

To automatically switch Git identities based on directory path (e.g. all agent workspaces use `onasafari`, while personal projects use `arii`):

### In `~/.gitconfig`:
```ini
[user]
    name = Ariel Anders
    email = anders.ariel@gmail.com

# Automatically override identity when in agent workspaces:
[includeIf "gitdir:~/portfolio/"]
    path = ~/.gitconfig-agent

[includeIf "gitdir:~/boomtick/"]
    path = ~/.gitconfig-agent
```

### In `~/.gitconfig-agent`:
```ini
[user]
    name = Antigravity Agent
    email = ariandersrobotics@gmail.com
    signingkey = BBB26C2C8E2C66BDD1C83621F33835F96989A742
[commit]
    gpgsign = true
```

Now any `git commit` inside `~/portfolio/` automatically uses the agent identity, while commits in other folders default to your personal identity.

---

## 5. Method 4: Multi-Account Management with GitHub CLI (`gh`)

The GitHub CLI natively supports multiple authenticated accounts:

### Step 1: Login with Both Accounts
```bash
# Login primary account
gh auth login

# Login secondary agent account
gh auth login --hostname github.com
```

### Step 2: Check Active Account
```bash
gh auth status
```
Output:
```text
github.com
  ✓ Logged in to github.com account arii (/home/ari/.config/gh/hosts.yml)
  - Active account: true

  ✓ Logged in to github.com account onasafari (/home/ari/.config/gh/hosts.yml)
  - Active account: false
```

### Step 3: Switch Active Account
```bash
# Switch to agent account
gh auth switch --user onasafari

# Switch back to primary account
gh auth switch --user arii
```

### Step 4: Environment Variable Override
In automation scripts or subagents, you can override the token for a specific command without switching global state:
```bash
GH_TOKEN="ghp_agent_personal_access_token..." gh pr create ...
```

---

## 6. Method 5: GitHub App / Bot Identity (`[bot]`)

When you want pull requests and commits to show up as a verified GitHub App (like `boomtick[bot]` or `dependabot[bot]`):

1. **Create a GitHub App** under your organization or personal account:
   - Go to **Settings** → **Developer settings** → **GitHub Apps** → **New GitHub App**.
   - Set permissions: Repository contents (Read & Write), Pull requests (Read & Write).
2. **Generate a Private Key** (`.pem` file) and note the **App ID**.
3. **Install the App** onto your target repositories.
4. **Use App Installation Tokens** in automation:
   - When committing via GitHub App token, use the GitHub bot author format:
     ```bash
     git config user.name "boomtick[bot]"
     git config user.email "306090700+boomtick[bot]@users.noreply.github.com"
     ```
   GitHub will recognize the ID and display the official `[bot]` badge.

---

## 7. Verification & Diagnostic Checklist

Use these commands to verify that your agent identity is operating properly:

```bash
# 1. Verify Git author attribution in current repo
git config user.name
# Expected: Antigravity Agent

git config user.email
# Expected: ariandersrobotics@gmail.com

# 2. Verify GPG signing key
git config user.signingkey
# Expected: BBB26C2C8E2C66BDD1C83621F33835F96989A742

# 3. Test GPG signing locally
echo "test" | gpg --clear-sign

# 4. Check commit log formatting
git log -n 1 --pretty=format:"Author: %an <%ae>%nCommit Hash: %h%nSignature: %G? (G=Good, B=Bad, U=Untrusted, N=None)"

# 5. Check remote SSH authentication
ssh -T git@github.com
```
