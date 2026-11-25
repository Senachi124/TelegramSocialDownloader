# Instagram partial GraphQL responses

Instagram can return a complete post and GraphQL errors for the optional
`ai_interactive_embodiment_attachment_style_info` decoration field. Previously
any error aborted the post as `missing_post_media`, despite valid login and
available media.

The adapter accepts only the two observed paths to this field, with the exact
known `field_exception` message and `ERROR` severity. It requires one post
matching the requested shortcode, numeric media identities, usable image/video
variants, and complete, uniquely identified carousel children. It does not
change the response or discard individual media items.

Unknown or mixed errors, incomplete media, identity mismatches, authentication
failures and rate limits still stop extraction. An informational log records
the number of ignored optional-field errors, without cookies or response bodies.
This is a compatibility exception for one field, not general GraphQL error
suppression. Existing error-free responses follow their previous path.

Offline regression tests cover both accepted paths, full mixed carousels,
unknown/critical/mixed errors, malformed indices, missing media, incomplete
carousels, changed post identities, and transport authentication/rate stops.
No schema or credential migration is required.
