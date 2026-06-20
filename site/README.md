# site/ — Internal intake console (static landing)

Single-file static page deployed to Vercel as the staff entry point. Links
into the live `/intakes` dashboard on the FastAPI app at
`https://api.felivoice.com/intakes`.

This folder is **independent of the FastAPI app**. Vercel deploys only this
folder; the Python app continues to run on the Mac under launchd. Vercel and
the FastAPI app share nothing except the GitHub repo.

## Deploy

When importing the repo into Vercel for the first time:

1. Vercel.com → **Add New → Project → Import** the `Feli-Voice` repo.
2. **Root Directory:** set to `site` (critical — without this Vercel will try
   to build the Python app, which won't work because the voice agent needs
   WebSockets that Vercel serverless does not support).
3. **Framework Preset:** Other.
4. Build / output settings: leave blank — `vercel.json` in this folder pins
   them and adds `X-Robots-Tag: noindex` so search engines skip the page.
5. Deploy.

Every `git push` to the repo's default branch triggers a redeploy. Changes
outside `site/` (the Python app) are ignored by Vercel.
