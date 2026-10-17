-- SPELL_ATTR0_CU_IGNORE_BINARY (512): retail logs show partial resists on these despite the debuff or knockback
UPDATE `spell_custom_attr` SET `attributes` = `attributes` | 512 WHERE `spell_id` = 64619; -- Water Spray (Mimiron's Emergency Fire Bot)
DELETE FROM `spell_custom_attr` WHERE `spell_id` IN (62311, 63038, 63830, 64596);
INSERT INTO `spell_custom_attr` (`spell_id`, `attributes`) VALUES
(62311, 512), -- Cosmic Smash (Algalon)
(63038, 512), -- Dark Volley (Yogg-Saron)
(63830, 512), -- Malady of the Mind (Yogg-Saron)
(64596, 512); -- Cosmic Smash (Algalon)
