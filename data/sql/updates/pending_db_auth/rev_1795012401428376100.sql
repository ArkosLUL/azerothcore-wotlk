--
DELETE FROM `rbac_permissions` WHERE `id` = 1000;
INSERT INTO `rbac_permissions` (`id`, `name`) VALUES
(1000, 'Command: group sated');

DELETE FROM `rbac_linked_permissions` WHERE `linkedId` = 1000;
INSERT INTO `rbac_linked_permissions` (`id`, `linkedId`) VALUES
(197, 1000);
