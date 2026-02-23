        logger.info(f"Adding tender: {tender.unique_name}")
        tender_id = str(tender.tender_id) if tender.tender_id else tender.unique_name or ""
        logger.debug(f"Tender ID: {tender_id}")