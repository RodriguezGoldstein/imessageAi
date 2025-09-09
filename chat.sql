SELECT
    m.rowid as message_id,
    (SELECT chat_id FROM chat_message_join WHERE chat_message_join.message_id = m.rowid) as message_group,
    CASE p.participant_count
        WHEN 0 THEN "???"
        WHEN 1 THEN "Individual"
        ELSE "Group"
    END AS chat_type,
    DATETIME(date +978307200, 'unixepoch', 'localtime') AS date,
    CASE is_from_me
        WHEN 0 THEN "Received"
        WHEN 1 THEN "Sent"
        ELSE is_from_me
    END AS type,
    id AS address,
    text,
    CASE cache_has_attachments
        WHEN 0 THEN Null
        WHEN 1 THEN filename
    END AS attachment,
    m.service
FROM message AS m
LEFT JOIN message_attachment_join AS maj ON message_id = m.rowid
LEFT JOIN attachment AS a ON a.rowid = maj.attachment_id
LEFT JOIN handle AS h ON h.rowid = m.handle_id
LEFT JOIN (SELECT count(*) as participant_count, cmj.chat_id, cmj.message_id as mid FROM 
    chat_handle_join as chj
    INNER JOIN chat_message_join as cmj on cmj.chat_id = chj.chat_id
    GROUP BY cmj.message_id, cmj.chat_id) as p on p.mid = m.rowid

ORDER BY date DESC