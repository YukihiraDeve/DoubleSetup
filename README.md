# SetupDouble

Utilise le **clavier, la souris et la Scarlett 2i2 branchés sur le PC Windows**
pour piloter le **MacBook**, par le réseau local, sans rien débrancher.

- **Ctrl + Alt + Espace** bascule le contrôle PC ⇄ Mac. Tu peux aussi passer par le menu de l'icône près de l'horloge Windows.
- Le **son du Mac** sort dans le casque branché sur la Scarlett, mélangé au son du PC.
- Le **micro de la Scarlett** est disponible sur le Mac (Discord, Meet, Zoom…).
- Le **presse-papiers texte** suit la bascule : tu copies sur une machine, tu colles sur l'autre.
- En mode PC, **rien n'intercepte le clavier ni la souris** : aucune latence ajoutée en jeu.
- Une **fenêtre de statut**, sur le Mac comme sur le PC, montre où sont le clavier et la souris et si tout fonctionne.

```
 PC Windows (serveur)                             MacBook (agent)
 ┌──────────────────────────┐   TCP 47801        ┌──────────────────────────┐
 │ clavier/souris ──hooks───┼──────────────────▶ │ injection Quartz (CGEvent)│
 │ (seulement en mode Mac)  │  touches, souris   │                          │
 │                          │                    │                          │
 │ Scarlett sortie ◀────────┼───── UDP 47802 ────┼── BlackHole 2ch (son Mac) │
 │ Scarlett entrée (micro) ─┼───── UDP 47803 ───▶│── BlackHole 16ch (micro)  │
 │ icône + raccourci        │                    │ LaunchAgent               │
 └──────────────────────────┘                    └──────────────────────────┘
```

## Installation

### 1. Sur le PC Windows

1. Copie le dossier du projet sur le PC (clé USB, partage réseau ou dépôt git).
2. Installe Python 3.12 (`winget install Python.Python.3.12`) ou `uv` (`winget install astral-sh.uv`).
3. Ouvre **PowerShell en administrateur** dans le dossier, puis :

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\install_windows.ps1
   ```

4. **Note le secret affiché** : il te servira sur le Mac.

Le script installe les dépendances et crée `config.toml`. Il ouvre aussi les ports dans le pare-feu, pour le réseau *privé* uniquement. Enfin, il lance SetupDouble à chaque ouverture de session, avec les droits élevés pour que le raccourci marche même devant une appli lancée en admin.

### 2. Sur le Mac

```bash
./scripts/install_mac.sh --secret LE_SECRET_DU_PC
```

Le script installe BlackHole 2ch, BlackHole 16ch et SwitchAudioSource. macOS demande ton mot de passe à ce moment. Le script installe aussi l'environnement Python et un LaunchAgent qui démarre l'agent automatiquement.

**Une étape reste manuelle** (macOS l'impose) : autoriser l'**Accessibilité**.
Le script affiche le chemin exact de Python à ajouter dans
*Réglages Système → Confidentialité et sécurité → Accessibilité → « + »*.
Ensuite, redémarre l'agent :

```bash
launchctl kickstart -k gui/$(id -u)/com.setupdouble.agent
```

**Conseillé** : ajoute la disposition **« Français – PC »** dans
*Réglages Système → Clavier → Méthodes de saisie*. Les touches sont envoyées
selon leur position physique, donc avec cette disposition AltGr, @, #, { } et
le reste tombent exactement comme sur Windows.

## Utilisation

| Action | Comment |
|---|---|
| Passer au Mac / revenir au PC | **Ctrl + Alt + Espace**, ou clic gauche sur l'icône |
| Choisir explicitement | clic droit sur l'icône → *Contrôler le Mac* / *Contrôler le PC* |
| Couper le son du Mac dans le casque | menu → *Son du Mac dans le casque* |
| Couper le micro vers le Mac | menu → *Micro vers le Mac* |
| Volume du casque | touches volume du clavier ou molette de la Scarlett (toujours gérés par Windows) |

L'icône affiche **PC** (gris) ou **MAC** (bleu). Une pastille verte indique que le Mac est connecté, une rouge qu'il ne l'est pas.

**Sécurité** : si le Mac se déconnecte (veille, Wi-Fi coupé…) pendant que tu le
contrôles, le clavier et la souris reviennent **automatiquement** au PC.

Raccourcis clavier côté Mac, avec `modifier_layout = "pc"` (défaut) :

| Touche du clavier PC | Sur le Mac |
|---|---|
| Ctrl | ⌘ Cmd (Ctrl+C, Ctrl+V, Ctrl+Tab… comme sur Windows) |
| Windows | ⌃ Ctrl |
| Alt | ⌥ Option |
| AltGr | ⌥ Option droite |

Avec `modifier_layout = "mac"`, tu retrouves la disposition Apple : Windows = Cmd, Ctrl = Ctrl.

## Fenêtre de statut

Elle montre en un coup d'œil :
- quelle machine utilise le clavier et la souris, avec un bouton pour basculer ;
- si le PC et le Mac sont connectés, et la latence entre eux ;
- si le son du Mac arrive bien dans le casque et si le micro arrive sur le Mac, avec un interrupteur pour chacun ;
- la liste des vérifications des deux machines (Accessibilité, BlackHole, Scarlett, raccourci). La pastille en haut passe au orange ou au rouge dès qu'un point bloque.

Pour l'ouvrir :
- **Mac** : l'app **SetupDouble** dans `~/Applications`, aussi accessible par Spotlight ou le Launchpad ;
- **PC** : menu Démarrer → **SetupDouble**, ou clic droit sur l'icône → *Ouvrir SetupDouble…*

La page est servie en local uniquement, sur `127.0.0.1:47811` côté Mac et `127.0.0.1:47810` côté PC. Elle est protégée par un jeton, donc un site web ne peut pas piloter la bascule.

Pour voir l'interface sans rien installer, lance la démo : elle simule un PC et un Mac connectés.

```bash
.venv/bin/python -m setupdouble.demo            # puis http://127.0.0.1:47821/ (vue Mac) ou :47820 (vue PC)
.venv/bin/python -m setupdouble.demo --window   # dans la fenêtre native macOS
```

## Configuration

Tout se règle dans `config.toml`, avec le même format sur les deux machines (voir
`config.example.toml`, commenté). Les réglages utiles :

- `hotkey_toggle`, `hotkey_mac`, `hotkey_pc` : les raccourcis, par exemple `"ctrl+alt+right"`.
- `server` (côté Mac) : l'IP du PC, si la découverte automatique ne marche pas.
- `mouse_speed`, `scroll_speed`, `invert_scroll` : le réglage de la souris sur le Mac.
- `latency_ms` : la taille du tampon audio. Monte-le à 50-80 ms si le son craque en Wi-Fi.
- `mic_channel` : l'entrée de la Scarlett où le micro est branché (1 ou 2).
- `win_output_device` / `win_input_device` : une partie du nom de la Scarlett sous Windows.

Pour lister les périphériques audio et vérifier ceux que la config va prendre :

```bash
.venv/bin/python -m setupdouble.devices          # Mac
```

```powershell
.venv\Scripts\python.exe -m setupdouble.devices  # Windows
```

## Dépannage

| Problème | Piste |
|---|---|
| Le Mac ne bouge pas, mais le journal dit « Connecté » | Accessibilité pas autorisée pour le bon Python (voir le chemin dans le journal) |
| « Le Mac n'est pas connecté » | Les deux machines sont-elles sur le même réseau ? Réseau Windows en *Privé* ? Sinon, mets l'IP du PC dans `client.server` |
| macOS demande l'accès au réseau local | Accepte (Réglages → Confidentialité → Réseau local) |
| Le son craque | Augmente `latency_ms` ; branche le Mac en Ethernet si possible |
| Pas de son du Mac dans le casque | Sortie du Mac = « BlackHole 2ch » ? Sous Windows, règle la Scarlett en 48 kHz |
| Les touches ² et < sont inversées | `swap_iso_keys = true` |
| Tester sans rien injecter | `.venv/bin/python -m setupdouble.client_mac --dry-run` |

Journaux :
- Windows : `%LOCALAPPDATA%\SetupDouble\server.log`, aussi accessible par *Ouvrir le journal* dans le menu.
- Mac : `~/Library/Logs/SetupDouble/client.log`

## Limites connues

- **Le trafic n'est pas chiffré.** L'accès est protégé par le secret partagé (HMAC) : sans lui, aucun appareil ne reçoit tes frappes. Mais quelqu'un qui écoute ton réseau local pourrait les lire. À réserver à un réseau domestique de confiance.
- L'audio passe par le réseau en PCM brut, avec environ 30 à 40 ms de latence : parfait pour la musique, la vidéo et les appels, pas pour enregistrer en t'écoutant en direct.
- L'état de Verr. Maj du Mac peut se désynchroniser si tu l'actives aussi depuis le clavier du Mac.
- Les touches volume restent volontairement sur Windows, puisque c'est lui qui alimente le casque.
- Si une appli Windows lancée en admin est au premier plan, il faut que SetupDouble tourne lui aussi en admin. Le script d'installation s'en occupe.

## Développement

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-mac.txt pytest
.venv/bin/python -m pytest -q
```

Les tests couvrent :
- le protocole ;
- la table des touches ;
- le tampon audio, y compris la compensation de dérive d'horloge ;
- un aller-retour audio UDP ;
- une intégration serveur ⇄ agent complète sur 127.0.0.1 ;
- le tableau de bord (état, bascule depuis le PC ou le Mac, refus sans jeton) ;
- la logique des hooks Windows, avec une `user32` simulée.

| Fichier | Rôle |
|---|---|
| `setupdouble/protocol.py` | trames TCP, paquets audio UDP, authentification, découverte |
| `setupdouble/server_core.py` | serveur côté PC (réseau, session, audio), indépendant de Windows |
| `setupdouble/win_input.py` | raccourci global + hooks bas niveau (ctypes) |
| `setupdouble/server_win.py` | point d'entrée Windows, icône dans la barre des tâches |
| `setupdouble/client_mac.py` | agent Mac |
| `setupdouble/mac_inject.py` | injection clavier/souris via Quartz |
| `setupdouble/mac_system.py` | presse-papiers et routage audio du Mac |
| `setupdouble/audio.py` | émission/réception audio, tampon anti-gigue |
| `setupdouble/keymap.py` | scancodes PC → keycodes Mac |
| `setupdouble/dashboard.py` | serveur local de la fenêtre de statut (état JSON + actions) |
| `setupdouble/ui/index.html` | l'interface de la fenêtre de statut |
| `setupdouble/app.py` | ouvre la fenêtre (WebKit natif sur Mac, Edge en mode appli sous Windows) |
| `setupdouble/mac_window.py`, `mac_bundle.py` | fenêtre macOS et création de `SetupDouble.app` |
