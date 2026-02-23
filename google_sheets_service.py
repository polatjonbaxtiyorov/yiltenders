# existing content of google_sheets_service.py 
# Add the following line before the logger.debug line

tender_id = str(tender.tender_id) if tender.tender_id else tender.unique_name or ''

# remaining content of google_sheets_service.py