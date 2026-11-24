<p align="center">
  <img src="setupdouble/ui/favicon.svg" width="88" alt="">
</p>

<h1 align="center">SetupDouble</h1>

<p align="center">
  <b>Un clavier, une souris, un casque, deux ordinateurs.</b><br>
  Passe d'une machine à l'autre avec un raccourci, par le réseau local, sans rien débrancher.
</p>

<p align="center">
  Windows · macOS · Linux — dans tous les sens
</p>

---

## En bref

- **Ctrl + Alt + Espace** envoie le clavier et la souris sur l'autre machine. Le même raccourci les ramène.
- **Le son de la machine pilotée** arrive dans ton casque, mélangé au son local ou seulement quand tu la pilotes, au choix.
- **Ton micro** est disponible sur la machine pilotée, tout le temps ou seulement quand tu la pilotes.
- **Le texte copié** suit la bascule.
- **Si la machine pilotée dort**, le raccourci la réveille.
- **Un seul écran pour deux machines ?** Il change d'entrée tout seul avec le raccourci.
- **Une fenêtre de statut**, façon macOS, montre où sont le clavier et la souris, si tout fonctionne, et permet de tout régler : raccourci, pistes audio, IP, clé.
- **En jeu**, rien n'intercepte le clavier tant que tu restes sur ta machine : aucune latence ajoutée.

Les deux rôles :

| Rôle | C'est la machine… |
|---|---|
| **Serveur** | où sont branchés le clavier, la souris et la carte son (ex. le PC gaming) |
| **Piloté** | que tu contrôles à distance (ex. le MacBook) |

Toutes les combinaisons marchent : Windows → macOS, Windows → Windows, macOS → macOS, Linux → macOS, macOS → Linux, etc.

## Installation

Il faut installer les deux machines, en commençant par le **serveur**. Son installation affiche une **clé**, à donner ensuite à la machine pilotée.

Récupère d'abord le projet sur chaque machine :

```bash
git clone https://github.com/YukihiraDeve/DoubleSetup.git
cd DoubleSetup
```

### Windows

Clic droit sur **`install.ps1`** → *Exécuter avec PowerShell*. Le script demande le rôle et les droits administrateur, puis s'occupe de tout : Python, pare-feu, démarrage automatique, raccourci dans le menu Démarrer.

### macOS et Linux

```bash
./install.sh
```

Le script pose deux questions : le rôle, puis la clé si la machine est pilotée.

Sur un Mac piloté, il installe aussi les pilotes audio BlackHole et active le réveil par le réseau.

Sur Linux, il donne l'accès au clavier et à la souris. Il faut se reconnecter une fois après l'installation.

### La seule étape manuelle (macOS)

macOS impose d'autoriser Python à contrôler le clavier et la souris. SetupDouble affiche la demande au premier lancement. Si elle n'apparaît pas :

*Réglages Système → Confidentialité et sécurité → Accessibilité* (« Contrôle de l'appareil et accès aux données » sur les versions récentes) → active **python3.12**.

> **Mac piloté avec un clavier PC AZERTY** : ajoute la disposition **« Français – PC »** dans *Réglages Système → Clavier → Méthodes de saisie*. AltGr, @, # et { } tombent alors comme sur Windows.

## Utilisation

| Je veux… | Comment |
|---|---|
| Basculer le clavier et la souris | **Ctrl + Alt + Espace**, ou le sélecteur de la fenêtre SetupDouble |
| Voir si tout marche | ouvrir **SetupDouble** (menu Démarrer, Spotlight/Launchpad, ou `python -m setupdouble app`) |
| Couper le son ou le micro partagé | les interrupteurs de la carte *Audio* |
| Choisir le casque, le micro, les pistes | bouton **Pistes audio** |
| Changer le raccourci, l'IP, la clé… | **⚙ Réglages** |
| Mettre à jour | Windows : clic droit sur l'icône → **Mettre à jour**. Ailleurs : `python -m setupdouble update` |

La pastille en haut de la fenêtre passe au **vert** quand tout est prêt. Sinon, la liste *Vérifications* dit quoi corriger, sur chaque machine.

**Sécurité** : si la machine pilotée se déconnecte (veille, Wi-Fi coupé…) pendant que tu la contrôles, le clavier et la souris reviennent **tout seuls** sur le serveur.

### Réglages

Tout se règle dans **⚙ Réglages** ; l'enregistrement redémarre SetupDouble.

| Onglet | Sur le serveur | Sur la machine pilotée |
|---|---|---|
| **Général** | nom, raccourcis (avec bouton « Enregistrer » : appuie sur la combinaison voulue), presse-papiers, réveil réseau | nom, touches Ctrl/Windows/⌘, vitesse du curseur et du défilement, veille |
| **Audio** | sortie casque, entrée micro, son « les deux en même temps » ou « suivre le contrôle », pareil pour le micro, volumes | capture du son, micro virtuel |
| **Connexion** | clé | IP du serveur (vide = automatique), clé |

Les mêmes réglages sont dans `config.toml`, commenté.

### Modes audio

- **Les deux en même temps** (défaut) : le son de la machine pilotée se mélange à celui du serveur dans le casque, en permanence.
- **Suivre le contrôle** : tu n'entends la machine pilotée que lorsque tu la pilotes. Sur un Mac, son son repasse sur ses haut-parleurs le reste du temps.
- Pour le **micro**, c'est le même choix : partagé en permanence (pratique pour un appel sur le Mac pendant que tu joues), ou seulement quand tu pilotes.

### Pistes audio de la machine pilotée

| Système | Capturer son son | Recevoir le micro |
|---|---|---|
| **macOS** | BlackHole 2ch (installé, routage automatique) | BlackHole 16ch (installé) |
| **Windows** | un câble virtuel ([VB-CABLE](https://vb-audio.com/Cable/) A+B) | « CABLE Input », puis « CABLE Output » comme micro dans tes applis |
| **Linux** | une source « Monitor » PipeWire/PulseAudio | un *null sink* PipeWire |

### Écran partagé

Si le même écran est branché sur les deux machines, SetupDouble peut lui faire changer d'entrée en même temps que le clavier et la souris. Il utilise pour ça la commande DDC/CI, que la plupart des écrans acceptent.

À régler sur **chaque** machine, dans *Réglages → Écran* :
1. Active **« Changer d'entrée de l'écran avec le raccourci »**.
2. Pendant que l'écran affiche cette machine, clique sur **Détecter**, ou choisis l'entrée dans la liste (HDMI 1, DisplayPort 1…).
3. Enregistre.

**Pour vérifier :** **Tester l'aller-retour** affiche l'autre machine 5 secondes puis revient.

**Si un côté ne peut pas commander l'écran** (dock ou adaptateur USB-C qui bloque le DDC), l'autre côté renvoie la commande en renfort. Cela suppose que l'écran accepte une commande venant de l'entrée qui n'est pas affichée : le test te le dit tout de suite.

| Système | Outil |
|---|---|
| Windows | intégré, rien à installer |
| macOS (Apple Silicon) | `m1ddc`, installé par `install.sh` |
| Linux | `ddcutil`, installé par `install.sh` |

### Machine pilotée endormie

- **Écran éteint** : tant que le serveur est connecté et la machine sur secteur, seul l'écran s'éteint, et le raccourci le rallume.
- **Vraiment endormie** : le serveur envoie un signal Wake-on-LAN, attend qu'elle se reconnecte, puis bascule. Le réveil fonctionne surtout en Ethernet. En Wi-Fi, il dépend du matériel.

## Dépannage

| Problème | Piste |
|---|---|
| Pastille « Non connecté » | Même réseau ? Réseau Windows en *Privé* ? Sinon, mets l'IP du serveur dans *Réglages → Connexion* de la machine pilotée |
| « Clé refusée » | La clé doit être identique sur les deux machines (*Réglages → Connexion*) |
| La machine pilotée ne bouge pas | macOS : Accessibilité. Linux : reconnecte-toi après l'installation (groupe `input`) |
| Le raccourci ne fait rien | Une autre appli l'utilise peut-être : la fenêtre l'indique en orange. Choisis-en un autre dans *Réglages* |
| Pas de son dans le casque | *Pistes audio* : bonne sortie ? Sur Mac piloté, BlackHole n'apparaît qu'après un redémarrage |
| Le son craque | *Réglages → Audio → Tampon audio* : monte à 50-80 ms, ou passe en Ethernet |
| ² et < inversés | *Réglages → Général → Inverser les touches ² et <* |
| L'écran ne change pas d'entrée | *Réglages → Écran* sur les deux machines : entrées définies ? Le test d'aller-retour indique quel côté peut commander l'écran. Évite les docks USB-C, qui bloquent souvent le DDC |

Journaux, accessibles aussi par « Ouvrir le journal » :
- Windows : `%LOCALAPPDATA%\SetupDouble\`
- macOS : `~/Library/Logs/SetupDouble/`
- Linux : `~/.local/state/setupdouble/`

## Limites connues

- **Le trafic n'est pas chiffré.** Seuls les appareils qui connaissent la clé peuvent se connecter, mais quelqu'un qui écoute le réseau pourrait lire les frappes. À réserver à un réseau domestique de confiance.
- **L'audio a 30 à 40 ms de retard** : parfait pour la musique, la vidéo et les appels, pas pour t'écouter en direct pendant un enregistrement.
- **Testé en vrai** : Windows → macOS. Les autres combinaisons (pilotes Windows et Linux, capture macOS et Linux) sont couvertes par les tests automatiques, mais pas encore éprouvées sur du vrai matériel.
- **Serveur Linux** : pas d'icône dans la barre système. La fenêtre de statut et le raccourci suffisent.
- **Serveur macOS ou Linux** : les lettres des raccourcis suivent leur position QWERTY. Préfère `space`, les flèches ou `f1..f24`.

## Développement

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-mac.txt pytest pyflakes
.venv/bin/python -m pytest -q
.venv/bin/python -m setupdouble demo           # interface avec un serveur et un client simulés
```

| Fichier | Rôle |
|---|---|
| `setupdouble/server.py`, `server_core.py` | serveur (capture, session, audio, réveil) |
| `setupdouble/client.py` | machine pilotée |
| `setupdouble/platforms.py` | tout ce qui dépend du système |
| `win_input.py` / `mac_capture.py` / `linux_input.py` | capture clavier et souris |
| `win_inject.py` / `mac_inject.py` / `linux_input.py` | injection clavier et souris |
| `setupdouble/dashboard.py`, `settings.py`, `ui/index.html` | fenêtre de statut et réglages |
| `setupdouble/protocol.py`, `audio.py`, `keymap.py` | réseau, audio, tables de touches |
| `setupdouble/display.py` | bascule de l'entrée de l'écran (DDC/CI) |
| `setupdouble/updater.py` | mise à jour depuis GitHub |
