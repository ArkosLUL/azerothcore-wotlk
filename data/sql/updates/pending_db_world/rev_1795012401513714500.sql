--
DELETE FROM `command` WHERE `name` = 'group sated';
INSERT INTO `command` (`name`, `security`, `help`) VALUES
('group sated', 2, 'Syntax: .group sated [$characterName]\r\nRemoves Sated and Exhaustion (the Bloodlust and Heroism debuffs) from every member of the given character''s group and their pets, or of your own group if no character is given.');
