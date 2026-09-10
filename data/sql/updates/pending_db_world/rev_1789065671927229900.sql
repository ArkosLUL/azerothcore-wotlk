--
DELETE FROM `command` WHERE `name` = 'group cooldown';
INSERT INTO `command` (`name`, `security`, `help`) VALUES
('group cooldown', 2, 'Syntax: .group cooldown [$characterName]\r\nRemoves all spell cooldowns from every member of the given character''s group, or of your own group if no character is given.');
