IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('dbo.Projects') AND name = 'StickerNumbers')
    ALTER TABLE dbo.Projects ADD StickerNumbers NVARCHAR(MAX) NULL;
