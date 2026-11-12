- **Calendar feeds with years of history now sync (#1182).** A secret iCal address exports the whole
  calendar, so a work calendar over 2 MB was refused on every sweep and none of its meetings
  imported. The cap is now 32 MB and operator-set (`CALENDAR_SYNC_MAX_ICS_BYTES`); the fetch streams
  and stops at the cap instead of downloading the feed first, and a sign-in page reports itself as
  a web page rather than as an oversized feed. See
  [Calendar sync](/how-to/calendar-sync#how-often-vexa-checks-the-feed).
