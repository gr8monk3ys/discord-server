#!/bin/sh
# SpotifyForge's weekly self-maintenance (front-desk-spotify-weekly.timer). Order matters:
#   stats    snapshot follower counts before anything changes
#   refresh  rotate one pinned track on a few playlists (local only)
#   reflow   push rotations + any drift, the single Spotify write path
#   describe keep descriptions current with the reflowed contents
# Every step runs even if an earlier one failed, and the job exits with the worst code, as it did on
# the PC. --min-size 4 --max-size 50 are canonical: any other sizes mis-plan the catalogue.
worst=0
step() {
    spotifyforge curate "$@"
    code=$?
    if [ "$code" -ne 0 ]; then
        echo "step '$*' exited $code"
        worst=$code
    fi
}

step stats
step refresh --limit 10 --min-size 4 --max-size 50
step reflow --harmonic --min-size 4 --max-size 50
step describe --harmonic --min-size 4 --max-size 50
exit "$worst"
