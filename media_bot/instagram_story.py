"""Select one active Story before gallery-dl prepares media transfers."""
from gallery_dl.extractor.instagram import InstagramStoriesExtractor


class SingleStoryExtractor(InstagramStoriesExtractor):
    unavailable = False

    def posts(self):
        if not self.user or self.highlight_id:
            raise self.exc.NotFoundError("Story author")
        owner_id = str(self.api.user(self.user)["id"])
        selected = None
        latest = (-1, -1)
        for reel in self.api.reels_media((owner_id,)):
            if str(reel["id"]) != owner_id:
                continue
            for item in reel["items"]:
                media_id = str(item.get("pk") or "")
                if not media_id.isascii() or not media_id.isdecimal():
                    continue
                if self.media_id and self.media_id != media_id:
                    continue
                if not (item.get("image_versions2") or {}).get("candidates"):
                    continue
                # A video without a video rendition must not become a cover image.
                if item.get("media_type") == 2 and not item.get("video_versions"):
                    continue
                if self.media_id:
                    return ({**reel, "items": [item]},)
                timestamp = str(item.get("taken_at") or "")
                rank = (int(timestamp) if timestamp.isdecimal() else 0, int(media_id))
                if rank > latest:
                    latest = rank
                    selected = {**reel, "items": [item]}
        if selected:
            return (selected,)
        self.unavailable = bool(self.media_id)
        return ()
