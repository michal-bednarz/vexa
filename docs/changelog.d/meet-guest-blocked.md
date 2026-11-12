- **Google Meet: a meeting that does not admit browser guests ends the join at once.** When Meet
  refuses a join, the reason text now carries Meet's own message. When that message says the
  meeting does not admit guests joining from a browser, the bot stops right away and no new bot is
  sent, instead of knocking again for minutes. The run ends `failed` / `platform_join_refused`
  rather than `awaiting_admission_rejected`, so it no longer reads as a host denial. See
  [Completion reasons](/troubleshooting/completion-reasons#a-join-google-meet-refuses-google_meet_join_refused).
