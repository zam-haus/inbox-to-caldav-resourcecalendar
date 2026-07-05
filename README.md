# inbox-to-caldav-resourcecalendar
A python script based on RFC 5546/6047 (iTIP/iMIP) that reads an inbox and fills caldav calendars with events. Can be used as a replacement for other resource calendars.


## Tested with

Caldav Server: Nextcloud

IMAP Server: Postfix

Calendar Clients / iMIP Mail Senders: Google, Nextcloud, Thunderbird

## Restrictions

### Nextcloud Trashcan

Importing an Event into a calendar, deleting it (Nextcloud moves the instance into the trash bin), importing it again (with the same UID) prevents the event from being deleted again both via CALDAV and via WebUI, because the trash bin cannot hold duplicates with the same UID.

Solution: We won't delete Events, only set them to Cancelled. Deletion must be performed by an autorized Nextcloud user manually.

### Inconsistent Behaviour when Editing Series with Edited Occurrences

When creating a calendar series, moving and deleting one occurrence each within the same day, and then editing the whole series to another time of day:

* Nextcloud Web UI hangs and does not update the series
* Thunderbird: Edits the unchanged events of the series, keeps the moved occurrence, and omits the series occurrences on the days of the moved and deleted occurrences.
* Business Calendar 2 on Android + Google Calendar in Android System: Edits the unchanged events of the series, keeps the moved occurrence and the new series occurrence, and omits the series occurrence on the day of the deleted occurrence.
* Google Calendar Web and Outlook on Windows: Edits the series, discards all deleted or moved occurrences and restores them to the original series time. 

Solution: We reject VEVENTS with RECURRENCE-ID set (which are edited occurrences). We still allow updates to the series with EXDATE (which are excluded dates/times). A user can delete an occurrence and create a new unrelated event on that day with another time and another UID as a workaround.

## Relevant Standards

https://datatracker.ietf.org/doc/html/rfc6047 / https://datatracker.ietf.org/doc/html/rfc2447 (iCalendar Message-Based Interoperability Protocol iMIP)

https://datatracker.ietf.org/doc/html/rfc5546 / https://datatracker.ietf.org/doc/html/rfc2446 (iCalendar Transport-Independent Interoperability Protocol iTIP)

## Similar works

- https://groupware.boddie.org.uk/imip-agent/ (based on Python 2)
