-- 04_reviews.sql — UC2, UC3: customer ratings linking eu_profiles to catalog.editions.
-- user_id values match customers.eu_profiles.user_id.
-- edition_id values match catalog.editions.edition_id (1-40).

CREATE TABLE reviews.user_ratings (
    rating_id           SERIAL PRIMARY KEY,
    user_id             VARCHAR(20)  NOT NULL,
    edition_id          INTEGER      NOT NULL,
    score               SMALLINT     NOT NULL CHECK (score BETWEEN 1 AND 5),
    review_text         TEXT,
    is_verified_purchase BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMP    NOT NULL DEFAULT NOW()
);

INSERT INTO reviews.user_ratings (user_id, edition_id, score, review_text, is_verified_purchase, created_at) VALUES
('user_101',  1,  5, 'Absolutely gripping from start to finish!',                    TRUE,  '2023-04-01 10:00:00'),
('user_102',  4,  5, 'Best sci-fi I have read this year!',                           TRUE,  '2023-07-10 11:20:00'),
('user_103',  5,  4, 'Sweet and well-written romance.',                              FALSE, '2023-02-14 08:00:00'),
('user_104',  7,  5, 'Epic fantasy — could not put it down.',                        TRUE,  '2022-12-25 09:00:00'),
('user_105', 10,  5, 'Fascinating history, well researched.',                        TRUE,  '2023-10-20 08:45:00'),
('user_106', 11,  4, 'Practical business advice.',                                   TRUE,  '2024-02-10 11:30:00'),
('user_107', 13,  4, 'Lovely memoir, very inspiring.',                               TRUE,  '2023-08-20 09:30:00'),
('user_108', 14,  5, 'My kids love this picture book!',                              TRUE,  '2023-05-15 07:00:00'),
('user_109', 15,  4, 'Clear explanations of complex topics.',                        TRUE,  '2024-03-20 13:00:00'),
('user_110', 17,  3, 'A bit dry in places.',                                         TRUE,  '2023-09-10 16:20:00'),
('user_111', 18,  4, 'Well-organized self-help tips.',                               FALSE, '2023-06-15 08:30:00'),
('user_112', 20,  5, 'Dragon Codex sequel delivers!',                                TRUE,  '2024-04-10 09:00:00'),
('user_113', 21,  4, 'Charming seaside romance.',                                    TRUE,  '2024-02-14 14:00:00'),
('user_114', 22,  4, 'Good thriller, well-paced.',                                   TRUE,  '2024-05-20 11:15:00'),
('user_115', 24,  5, 'Mind-bending sci-fi concept!',                                 TRUE,  '2024-07-20 08:00:00'),
('user_116', 25,  5, 'Kids asked me to read it three times.',                        TRUE,  '2024-03-10 07:30:00'),
('user_117', 26,  4, 'Solid popular science book.',                                  TRUE,  '2024-06-01 10:45:00'),
('user_118', 28,  3, 'Some chapters felt repetitive.',                               FALSE, '2024-08-15 09:00:00'),
('user_119', 29,  4, 'Beautiful literary fiction.',                                   TRUE,  '2024-09-10 12:30:00'),
('user_120', 30,  5, 'Fun middle-grade adventure.',                                  TRUE,  '2024-04-20 08:15:00'),
('user_101',  2,  4, 'Great paperback quality, good price.',                         TRUE,  '2023-10-01 09:00:00'),
('user_102',  3,  5, 'Loved the hard sci-fi elements.',                              TRUE,  '2023-08-15 14:20:00'),
('user_103',  6,  5, 'Audiobook narration was excellent.',                           TRUE,  '2023-05-01 12:30:00'),
('user_104',  8,  4, 'Good paperback edition, nice cover art.',                      TRUE,  '2023-06-10 10:15:00'),
('user_105', 12,  3, 'eBook had some formatting issues.',                            FALSE, '2022-12-01 14:00:00'),
('user_106', 16,  5, 'Perfect intro to physics.',                                    FALSE, '2024-03-25 10:00:00'),
('user_107', 19,  5, 'Audiobook was a joy to listen to.',                            TRUE,  '2023-09-01 12:00:00'),
('user_108', 23,  3, 'eBook formatting could be better.',                            FALSE, '2024-05-25 15:30:00'),
('user_109', 27,  4, 'eBook version was very readable.',                             FALSE, '2024-06-05 14:00:00'),
('user_110', 31,  5, 'Brilliant exploration of AI.',                                 TRUE,  '2024-10-05 11:00:00'),
('user_111', 32,  5, 'Perfect ending to the trilogy.',                               TRUE,  '2024-11-20 09:30:00'),
('user_112', 33,  4, 'eBook was a quick, satisfying read.',                          FALSE, '2024-11-25 14:00:00'),
('user_113', 34,  4, 'Sweet and funny dating app story.',                            TRUE,  '2024-12-05 10:00:00'),
('user_114', 35,  5, 'Edge-of-seat cyber thriller.',                                 TRUE,  '2025-01-10 08:45:00'),
('user_115', 36,  4, 'Learned so much about ancient history.',                       TRUE,  '2024-07-25 13:00:00'),
('user_116', 37,  3, 'Good startup advice, not groundbreaking.',                     FALSE, '2025-02-10 11:30:00'),
('user_117', 38,  5, 'Poetic and haunting.',                                         TRUE,  '2025-03-15 09:00:00'),
('user_118', 39,  5, 'Adorable space puppies!',                                      TRUE,  '2025-02-14 07:00:00'),
('user_119', 40,  4, 'Hardcover quality is superb.',                                 TRUE,  '2025-05-01 10:30:00'),
('user_120',  1,  4, 'Re-read it, still great.',                                     TRUE,  '2024-01-10 08:00:00'),
('user_101',  9,  4, 'eBook had some minor glitches but content is excellent.',      FALSE, '2022-12-01 14:00:00'),
('user_102', 11,  4, 'Decent paperback quality.',                                    FALSE, '2023-10-01 09:00:00'),
('user_103', 14,  5, 'A bedtime favorite for my children.',                          TRUE,  '2023-07-04 07:15:00'),
('user_104', 20,  4, 'Solid sequel, high expectations met.',                         TRUE,  '2024-05-01 10:00:00'),
('user_105', 24,  5, 'Chen does it again — phenomenal.',                             TRUE,  '2024-08-10 08:30:00'),
('user_106', 32,  4, 'eBook was well-formatted.',                                    FALSE, '2024-12-15 15:00:00'),
('user_107', 35,  5, 'Cannot wait for the next Vasquez book.',                       TRUE,  '2025-02-01 09:00:00'),
('user_108', 10,  4, 'Impressive historical research.',                               TRUE,  '2023-12-01 09:30:00'),
('user_109',  7,  4, 'Dragon Codex keeps getting better.',                           FALSE, '2023-06-30 11:00:00'),
('user_110',  4,  5, 'Epic fantasy, beautiful illustrations inside.',                TRUE,  '2023-01-15 10:00:00');

COMMENT ON TABLE reviews.user_ratings IS 'Customer ratings for specific book editions. Joins to customers.eu_profiles via user_id and to catalog.editions via edition_id, forming two cross-dataset join paths exploited by UC3 ontology generation.';

COMMENT ON COLUMN reviews.user_ratings.rating_id IS 'Synthetic primary key — auto-incrementing sequence, not meaningful outside this table.';
COMMENT ON COLUMN reviews.user_ratings.user_id IS 'Identifier of the reviewing customer. Matches customers.eu_profiles.user_id — the FK relationship is enforced by value alignment rather than a constraint to simplify seed ordering.';
COMMENT ON COLUMN reviews.user_ratings.edition_id IS 'Identifier of the rated edition. Matches catalog.editions.edition_id — the FK relationship is enforced by value alignment rather than a constraint to simplify seed ordering.';
COMMENT ON COLUMN reviews.user_ratings.score IS 'Star rating on a 1–5 scale. 1 = very poor, 5 = excellent. NOT NULL — records without a score are discarded at ingestion time.';
COMMENT ON COLUMN reviews.user_ratings.review_text IS 'Free-text review body written by the customer. NULL when the customer submitted only a star rating.';
COMMENT ON COLUMN reviews.user_ratings.is_verified_purchase IS 'TRUE when Imazon can confirm the reviewer purchased this edition. Influences UC2 completeness checks.';
COMMENT ON COLUMN reviews.user_ratings.created_at IS 'Timestamp when the rating was submitted (UTC). Used for recency weighting in recommendation models.';
